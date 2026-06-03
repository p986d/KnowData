from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any

from src.config import load_settings
from src.llm.llm_client import LLMClient
from src.llm.reasoning import REASONING_MODE_MAP, apply_reasoning_mode
from src.prompt.prompt_builder import PromptBuilder
from src.utils.run_log import build_timestamp


DEFAULT_PROMPT_DIR = Path(__file__).with_name("prompt_templates")
DEFAULT_LOG_ROOT = Path("log/knowdata_buildtopic")
DEFAULT_OUTPUT_DIR_NAME = "buildtopic"
NL2ER_FILENAME = "nl2er.json"
GLOBAL_ER_FILENAME = "global_er.json"
TOPICS_FILENAME = "topics.json"
SUMMARY_FILENAME = "buildtopic_summary.json"
ALIGNMENT_STEPS_FILENAME = "alignment_steps.json"

JSON_FENCE_RE = re.compile(r"```json\s*(.*?)\s*```", flags=re.IGNORECASE | re.DOTALL)


def resolve_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser()
    if resolved.is_absolute():
        return resolved.resolve()
    return (Path.cwd() / resolved).resolve()


def read_json_object(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object at {file_path}, got {type(payload).__name__}.")
    return payload


def write_json(path: str | Path, payload: Any) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_path


def parse_json_object(raw_text: str) -> dict[str, Any]:
    matches = JSON_FENCE_RE.findall(raw_text or "")
    candidate = matches[-1] if matches else raw_text
    try:
        payload = json.loads(str(candidate or "").strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse LLM JSON response: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Expected LLM JSON object, got {type(payload).__name__}.")
    return payload


def unique_strings(values: Any) -> list[str]:
    raw_items = values if isinstance(values, list) else [values]
    output: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


def normalize_attribute(attribute: Any) -> dict[str, Any] | None:
    if isinstance(attribute, str):
        name = attribute.strip()
        return {"local_name": name, "name": name, "semantics": ""} if name else None
    if not isinstance(attribute, dict):
        return None
    name = str(
        attribute.get("name")
        or attribute.get("attribute_name")
        or attribute.get("attr")
        or attribute.get("field_name")
        or ""
    ).strip()
    semantics = str(
        attribute.get("semantics")
        or attribute.get("semantic")
        or attribute.get("desc")
        or attribute.get("description")
        or ""
    ).strip()
    if not name and not semantics:
        return None
    return {
        "local_name": name,
        "name": name,
        "semantics": semantics,
        "raw": attribute,
    }


def normalize_attributes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    output: list[dict[str, Any]] = []
    for item in value:
        normalized = normalize_attribute(item)
        if normalized is not None:
            output.append(normalized)
    return output


def entity_name(entity: dict[str, Any]) -> str:
    return str(entity.get("entity_name") or entity.get("name") or "").strip()


def relation_name(relation: dict[str, Any]) -> str:
    return str(relation.get("relation_name") or relation.get("connection_name") or relation.get("name") or "").strip()


def participant_entity(participant: dict[str, Any]) -> str:
    return str(participant.get("entity") or participant.get("entity_name") or "").strip()


def normalize_case(input_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    question_id = str(payload.get("question_id") or input_path.parent.name).strip()
    entities: list[dict[str, Any]] = []
    for index, entity in enumerate(payload.get("entities") or [], start=1):
        if not isinstance(entity, dict):
            continue
        name = entity_name(entity)
        if not name:
            continue
        entities.append(
            {
                "local_id": f"{question_id}:entity:{index}",
                "question_id": question_id,
                "local_name": name,
                "desc": str(entity.get("desc") or entity.get("description") or "").strip(),
                "grain": str(entity.get("grain") or "").strip(),
                "attributes": normalize_attributes(entity.get("attributes")),
                "raw": entity,
            }
        )

    relations: list[dict[str, Any]] = []
    for index, relation in enumerate(payload.get("relations") or [], start=1):
        if not isinstance(relation, dict):
            continue
        name = relation_name(relation)
        participants: list[dict[str, Any]] = []
        for participant in relation.get("participants") or []:
            if not isinstance(participant, dict):
                continue
            entity = participant_entity(participant)
            if not entity:
                continue
            participants.append(
                {
                    "entity_local_name": entity,
                    "role": str(participant.get("role") or "").strip(),
                    "raw": participant,
                }
            )
        if not name and not participants:
            continue
        relations.append(
            {
                "local_id": f"{question_id}:relation:{index}",
                "question_id": question_id,
                "local_name": name,
                "relation_type": str(relation.get("relation_type") or "").strip(),
                "desc": str(relation.get("desc") or relation.get("description") or "").strip(),
                "grain": str(relation.get("grain") or "").strip(),
                "participants": participants,
                "attributes": normalize_attributes(relation.get("attributes")),
                "raw": relation,
            }
        )

    return {
        "question_id": question_id,
        "db_id": str(payload.get("db_id") or "").strip(),
        "question": str(payload.get("user_intent") or payload.get("question") or "").strip(),
        "input_path": str(input_path),
        "entities": entities,
        "relations": relations,
    }


def discover_nl2er_files(metadata_path: str | Path) -> list[Path]:
    root = resolve_path(metadata_path)
    if not root.exists():
        raise FileNotFoundError(f"metadata_path does not exist: {root}")
    if root.is_file():
        if root.name != NL2ER_FILENAME:
            raise ValueError(f"Expected a `{NL2ER_FILENAME}` file, got {root}.")
        return [root]
    direct = root / NL2ER_FILENAME
    if direct.exists():
        return [direct]
    paths = sorted(path for path in root.rglob(NL2ER_FILENAME) if path.is_file())
    if not paths:
        raise FileNotFoundError(f"No `{NL2ER_FILENAME}` files were found under {root}.")
    return paths


def mapping_key(question_id: str, local_name: str) -> tuple[str, str]:
    return (str(question_id).strip(), str(local_name).strip().casefold())


def relation_mapping_key(
    question_id: str,
    local_name: str,
    participant_entity_ids: list[str],
) -> tuple[str, str, tuple[str, ...]]:
    return (
        str(question_id).strip(),
        str(local_name).strip().casefold(),
        tuple(str(item).strip() for item in participant_entity_ids if str(item).strip()),
    )


def dedupe_by_id(items: list[dict[str, Any]], id_key: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get(id_key) or "").strip()
        if not item_id or item_id in seen:
            continue
        normalized = dict(item)
        normalized[id_key] = item_id
        output.append(normalized)
        seen.add(item_id)
    return output


def merge_attribute_payloads(*payloads: dict[str, Any]) -> dict[str, Any]:
    attributes: list[dict[str, Any]] = []
    mappings: list[dict[str, Any]] = []
    for payload in payloads:
        payload_attributes = [
            item
            for item in payload.get("canonical_attributes") or []
            if isinstance(item, dict)
        ]
        attributes.extend(payload_attributes)
        mappings.extend(BuildTopicRunner._attribute_mapping_items(payload))
    return {
        "canonical_attributes": dedupe_by_id(attributes, "attribute_id"),
        "attribute_mappings": mappings,
    }


def strip_trace_fields(item: dict[str, Any]) -> dict[str, Any]:
    trace_keys = {
        "local_refs",
        "local_names",
        "local_entities",
        "local_relations",
        "local_attributes",
        "question_ids",
    }
    return {key: value for key, value in item.items() if key not in trace_keys}


def normalized_flag_name(name: str) -> str:
    text = str(name or "").strip().casefold()
    for prefix in ("is_", "has_"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    for suffix in ("_related", "_based", "_level", "_type", "_status"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    replacements = {
        "permanent_residence": "permanent_resident",
        "household_registration": "household_registered",
        "community": "committee",
    }
    return replacements.get(text, text)


def is_entity_attribute_duplicate(name: str, entity_attribute_names: set[str]) -> bool:
    normalized = normalized_flag_name(name)
    if not normalized:
        return False
    return any(
        normalized == existing
        or normalized.startswith(f"{existing}_")
        or existing.startswith(f"{normalized}_")
        for existing in entity_attribute_names
    )


def first_string(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def list_from_keys(item: dict[str, Any], *keys: str) -> list[Any]:
    for key in keys:
        value = item.get(key)
        if isinstance(value, list):
            return value
    return []


class BuildTopicRunner:
    def __init__(
        self,
        *,
        llm: Any | None = None,
        model_config: str | None = None,
        reasoning_mode: str | None = None,
        prompt_dir: str | Path = DEFAULT_PROMPT_DIR,
        max_attribute_workers: int = 8,
        log_dir: str | Path | None = None,
    ) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.model_config = model_config
        self.reasoning_mode = reasoning_mode
        self.max_attribute_workers = max(1, int(max_attribute_workers))
        self.log_dir = Path(log_dir).resolve() if log_dir is not None else None
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        if llm is None:
            settings = load_settings()
            llm_config = apply_reasoning_mode(settings.llm.get(model_config), reasoning_mode)
            llm = LLMClient(llm_config)
        self.llm = llm
        self.prompt_builder = PromptBuilder(template_dir=self.prompt_dir, strict_undefined=True)
        self._register_prompts()

    def _register_prompts(self) -> None:
        self.prompt_builder.register_template(
            name="entity_alignment",
            template_name="entity_alignment.md",
            required_vars=["payload"],
        )
        self.prompt_builder.register_template(
            name="relation_alignment",
            template_name="relation_alignment.md",
            required_vars=["payload"],
        )
        self.prompt_builder.register_template(
            name="attribute_alignment",
            template_name="attribute_alignment.md",
            required_vars=["payload"],
        )
        self.prompt_builder.register_template(
            name="topic_clustering",
            template_name="topic_clustering.md",
            required_vars=["payload"],
        )

    @staticmethod
    def _format_payload(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _build_prompt(self, template_name: str, payload: dict[str, Any]) -> str:
        return self.prompt_builder.build_text(
            template_name,
            vars={"payload": self._format_payload(payload)},
        )

    @staticmethod
    def _question_ref(case: dict[str, Any]) -> dict[str, str]:
        return {
            "question_id": str(case.get("question_id") or ""),
            "question": str(case.get("question") or ""),
        }

    @staticmethod
    def _append_unique_question_ref(entry: dict[str, Any], case: dict[str, Any]) -> None:
        refs = entry.setdefault("question_refs", [])
        question_id = str(case.get("question_id") or "")
        if any(str(item.get("question_id") or "") == question_id for item in refs if isinstance(item, dict)):
            return
        refs.append(BuildTopicRunner._question_ref(case))

    def _entity_alignment_payload(self, cases: list[dict[str, Any]]) -> dict[str, Any]:
        by_definition: dict[tuple[str, str, str], dict[str, Any]] = {}
        for case in cases:
            question_id = str(case["question_id"])
            for entity in case["entities"]:
                local_name = str(entity.get("local_name") or "").strip()
                desc = str(entity.get("desc") or "").strip()
                grain = str(entity.get("grain") or "").strip()
                key = (local_name.casefold(), desc, grain)
                entry = by_definition.setdefault(
                    key,
                    {
                        "local_name": local_name,
                        "desc": desc,
                        "grain": grain,
                        "question_refs": [],
                    },
                )
                self._append_unique_question_ref(entry, case)
        return {
            "stage": "entity_alignment",
            "local_entities": list(by_definition.values()),
        }

    def _relation_alignment_payload(
        self,
        cases: list[dict[str, Any]],
        entity_payload: dict[str, Any],
        rewritten_relations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        cases_by_question = {str(case["question_id"]): case for case in cases}
        canonical_entity_context = self._canonical_entity_context(entity_payload)
        by_definition: dict[tuple[Any, ...], dict[str, Any]] = {}
        for relation in rewritten_relations:
            question_id = str(relation.get("question_id") or "")
            local_name = str(relation.get("local_name") or "").strip()
            desc = str(relation.get("desc") or "").strip()
            grain = str(relation.get("grain") or "").strip()
            participants = [
                {
                    "local_entity_name": str(item.get("local_entity_name") or ""),
                    "entity_id": str(item.get("entity_id") or ""),
                    "canonical_entity_name": str(item.get("canonical_entity_name") or ""),
                    "role": str(item.get("role") or ""),
                }
                for item in relation.get("participants") or []
                if isinstance(item, dict)
            ]
            participant_key = tuple(
                (
                    item.get("entity_id", ""),
                    item.get("canonical_entity_name", ""),
                    item.get("role", ""),
                )
                for item in participants
            )
            key = (
                local_name.casefold(),
                str(relation.get("relation_type") or "").strip(),
                desc,
                grain,
                participant_key,
            )
            entry = by_definition.setdefault(
                key,
                {
                    "local_name": local_name,
                    "relation_type": str(relation.get("relation_type") or "").strip(),
                    "desc": desc,
                    "grain": grain,
                    "participants": participants,
                    "question_refs": [],
                },
            )
            case = cases_by_question.get(question_id)
            if case is not None:
                self._append_unique_question_ref(entry, case)
        return {
            "stage": "relation_alignment",
            "canonical_entities": canonical_entity_context,
            "local_relations": list(by_definition.values()),
        }

    def _canonical_entity_context(self, entity_payload: dict[str, Any]) -> list[dict[str, Any]]:
        attrs_by_owner = self._attributes_by_owner(entity_payload)
        output: list[dict[str, Any]] = []
        for entity in entity_payload.get("canonical_entities") or []:
            if not isinstance(entity, dict):
                continue
            entity_id = str(entity.get("entity_id") or "").strip()
            if not entity_id:
                continue
            item = strip_trace_fields(entity)
            item["attributes"] = [
                self._canonical_entity_attribute_context(attr)
                for attr in attrs_by_owner.get(("entity", entity_id), [])
            ]
            output.append(item)
        return output

    @staticmethod
    def _canonical_entity_attribute_context(attr: dict[str, Any]) -> dict[str, Any]:
        output = strip_trace_fields(attr)
        output.pop("aliases", None)
        return output

    def _write_llm_markdown_logs(
        self,
        template_name: str,
        prompt: str,
        response: str,
        *,
        item_index: int | None = None,
    ) -> None:
        if self.log_dir is None:
            return
        if item_index is None:
            prompt_path = self.log_dir / f"{template_name}_prompt.md"
            response_path = self.log_dir / f"{template_name}_response.md"
        else:
            stage_dir = self.log_dir / template_name
            stage_dir.mkdir(parents=True, exist_ok=True)
            stem = f"{template_name}_{item_index + 1:04d}"
            prompt_path = stage_dir / f"{stem}_prompt.md"
            response_path = stage_dir / f"{stem}_response.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        response_path.write_text(response, encoding="utf-8")

    def _call_json(self, template_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        prompt = self._build_prompt(template_name, payload)
        raw_response = self.llm.single_turn(prompt)
        self._write_llm_markdown_logs(template_name, prompt, str(raw_response or ""))
        if not str(raw_response or "").strip():
            raise RuntimeError(f"LLM returned empty response for {template_name}.")
        return parse_json_object(str(raw_response))

    def _call_json_batch(
        self,
        template_name: str,
        payloads: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        prompts = [self._build_prompt(template_name, payload) for payload in payloads]
        if hasattr(self.llm, "batch_single_turn"):
            raw_outputs = self.llm.batch_single_turn(
                prompts,
                max_concurrency=self.max_attribute_workers,
            )
        else:
            raw_outputs = [self.llm.single_turn(prompt) for prompt in prompts]
        results: list[dict[str, Any]] = []
        for index, raw_response in enumerate(raw_outputs):
            self._write_llm_markdown_logs(
                template_name,
                prompts[index],
                str(raw_response or ""),
                item_index=index,
            )
            if not str(raw_response or "").strip():
                raise RuntimeError(
                    f"LLM returned empty response for {template_name}[{index}]."
                )
            results.append(parse_json_object(str(raw_response)))
        return results

    def load_cases(self, metadata_path: str | Path) -> list[dict[str, Any]]:
        cases = [
            normalize_case(path, read_json_object(path))
            for path in discover_nl2er_files(metadata_path)
        ]
        if not cases:
            raise ValueError("No NL2ER cases were loaded.")
        return cases

    def align_entities(self, cases: list[dict[str, Any]]) -> dict[str, Any]:
        return self._call_json("entity_alignment", self._entity_alignment_payload(cases))

    def rewrite_relation_participants(
        self,
        cases: list[dict[str, Any]],
        entity_payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        entity_map = self._entity_mapping_index(entity_payload)
        canonical_by_id = {
            str(item.get("entity_id") or "").strip(): item
            for item in entity_payload.get("canonical_entities") or []
            if isinstance(item, dict) and str(item.get("entity_id") or "").strip()
        }

        rewritten: list[dict[str, Any]] = []
        for case in cases:
            question_id = str(case["question_id"])
            for relation in case["relations"]:
                participants: list[dict[str, Any]] = []
                for participant in relation["participants"]:
                    local_name = str(participant.get("entity_local_name") or "").strip()
                    entity_id = entity_map.get(mapping_key(question_id, local_name), "")
                    canonical_entity = canonical_by_id.get(entity_id, {})
                    participants.append(
                        {
                            "local_entity_name": local_name,
                            "entity_id": entity_id,
                            "canonical_entity_name": str(canonical_entity.get("name") or ""),
                            "role": str(participant.get("role") or "").strip(),
                        }
                    )
                rewritten.append(
                    {
                        "question_id": question_id,
                        "question": case["question"],
                        "local_id": relation["local_id"],
                        "local_name": relation["local_name"],
                        "relation_type": relation["relation_type"],
                        "desc": relation["desc"],
                        "grain": relation["grain"],
                        "participants": participants,
                        "attributes": relation["attributes"],
                    }
                )
        return rewritten

    def align_relations(
        self,
        cases: list[dict[str, Any]],
        entity_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        rewritten_relations = self.rewrite_relation_participants(cases, entity_payload)
        relation_payload = self._call_json(
            "relation_alignment",
            self._relation_alignment_payload(cases, entity_payload, rewritten_relations),
        )
        return relation_payload, rewritten_relations

    @staticmethod
    def collect_alignment_attributes(
        entity_payload: dict[str, Any],
        relation_payload: dict[str, Any],
    ) -> dict[str, Any]:
        return merge_attribute_payloads(
            BuildTopicRunner._filter_entity_attribute_mappings(entity_payload),
            BuildTopicRunner._filter_relation_attribute_mappings(relation_payload, entity_payload),
        )

    @staticmethod
    def _filter_entity_attribute_mappings(entity_payload: dict[str, Any]) -> dict[str, Any]:
        entity_mappings = BuildTopicRunner._entity_mapping_index(entity_payload)
        filtered_attributes: list[dict[str, Any]] = []
        for attr in entity_payload.get("canonical_attributes") or []:
            if not isinstance(attr, dict):
                continue
            owner_id = str(attr.get("owner_id") or "").strip()
            refs = []
            for ref in BuildTopicRunner._local_ref_items(
                attr,
                "local_refs",
                "local_names",
                "local_attributes",
            ):
                question_id = str(ref.get("question_id") or "").strip()
                local_name = str(ref.get("local_name") or "").strip()
                mapped_owner_id = entity_mappings.get(mapping_key(question_id, local_name), "")
                if mapped_owner_id and owner_id and mapped_owner_id != owner_id:
                    continue
                refs.append(ref)
            filtered_attr = dict(attr)
            if refs:
                filtered_attr["local_refs"] = refs
            filtered_attributes.append(filtered_attr)

        filtered_mappings: list[dict[str, Any]] = []
        for item in entity_payload.get("attribute_mappings") or []:
            if not isinstance(item, dict):
                continue
            question_id = str(item.get("question_id") or "").strip()
            local_name = str(item.get("local_name") or "").strip()
            owner_id = str(item.get("owner_id") or "").strip()
            mapped_owner_id = entity_mappings.get(mapping_key(question_id, local_name), "")
            if mapped_owner_id and owner_id and mapped_owner_id != owner_id:
                continue
            filtered_mappings.append(item)
        return {
            "canonical_attributes": filtered_attributes,
            "attribute_mappings": filtered_mappings,
        }

    @staticmethod
    def _filter_relation_attribute_mappings(
        relation_payload: dict[str, Any],
        entity_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entity_attribute_names = {
            normalized_flag_name(str(item.get("name") or ""))
            for item in (entity_payload or {}).get("canonical_attributes") or []
            if isinstance(item, dict) and normalized_flag_name(str(item.get("name") or ""))
        }
        relation_by_local: dict[tuple[str, str], str] = {}
        for item in BuildTopicRunner._relation_mapping_items(relation_payload):
            if not isinstance(item, dict):
                continue
            question_id = first_string(item, "question_id", "qid", "source_question_id")
            local_name = first_string(
                item,
                "local_name",
                "local_relation",
                "local_relation_name",
                "source_name",
                "name",
            )
            relation_id = first_string(
                item,
                "relation_id",
                "canonical_relation_id",
                "target_relation_id",
                "canonical_id",
            )
            if question_id and local_name and relation_id:
                relation_by_local[mapping_key(question_id, local_name)] = relation_id

        filtered_attributes: list[dict[str, Any]] = []
        for attr in relation_payload.get("canonical_attributes") or []:
            if not isinstance(attr, dict):
                continue
            attr_name = str(attr.get("name") or "").strip().casefold()
            if is_entity_attribute_duplicate(attr_name, entity_attribute_names):
                continue
            owner_id = str(attr.get("owner_id") or "").strip()
            refs = []
            for ref in BuildTopicRunner._local_ref_items(
                attr,
                "local_refs",
                "local_names",
                "local_attributes",
            ):
                question_id = str(ref.get("question_id") or "").strip()
                local_name = str(ref.get("local_name") or "").strip()
                mapped_owner_id = relation_by_local.get(mapping_key(question_id, local_name), "")
                if mapped_owner_id and owner_id and mapped_owner_id != owner_id:
                    continue
                refs.append(ref)
            filtered_attr = dict(attr)
            if refs:
                filtered_attr["local_refs"] = refs
            filtered_attributes.append(filtered_attr)

        filtered_mappings: list[dict[str, Any]] = []
        for item in relation_payload.get("attribute_mappings") or []:
            if not isinstance(item, dict):
                continue
            question_id = str(item.get("question_id") or "").strip()
            local_name = str(item.get("local_name") or "").strip()
            owner_id = str(item.get("owner_id") or "").strip()
            mapped_owner_id = relation_by_local.get(mapping_key(question_id, local_name), "")
            if mapped_owner_id and owner_id and mapped_owner_id != owner_id:
                continue
            filtered_mappings.append(item)
        return {
            "canonical_attributes": filtered_attributes,
            "attribute_mappings": filtered_mappings,
        }

    @staticmethod
    def _attributes_by_owner(attribute_payload: dict[str, Any]) -> dict[tuple[str, str], list[dict[str, Any]]]:
        by_owner: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in attribute_payload.get("canonical_attributes") or []:
            if not isinstance(item, dict):
                continue
            owner_type = str(item.get("owner_type") or "").strip()
            owner_id = str(item.get("owner_id") or "").strip()
            if owner_type and owner_id:
                by_owner.setdefault((owner_type, owner_id), []).append(item)
        return by_owner

    def build_attribute_owner_payloads(
        self,
        cases: list[dict[str, Any]],
        entity_payload: dict[str, Any],
        relation_payload: dict[str, Any],
        rewritten_relations: list[dict[str, Any]],
        alignment_attribute_payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        entity_mappings = self._entity_mapping_index(entity_payload)
        relation_mappings = self._relation_mapping_index(relation_payload)
        existing_attrs_by_owner = self._attributes_by_owner(alignment_attribute_payload)

        entity_attrs_by_owner: dict[str, list[dict[str, Any]]] = {}
        for case in cases:
            question_id = str(case["question_id"])
            for entity in case["entities"]:
                entity_id = entity_mappings.get(mapping_key(question_id, entity["local_name"]), "")
                if not entity_id:
                    continue
                for attr in entity["attributes"]:
                    entity_attrs_by_owner.setdefault(entity_id, []).append(
                        {
                            "question_id": question_id,
                            "entity_local_name": entity["local_name"],
                            **attr,
                        }
                    )

        relation_attrs_by_owner: dict[str, list[dict[str, Any]]] = {}
        for relation in rewritten_relations:
            question_id = str(relation["question_id"])
            participant_ids = [
                str(item.get("entity_id") or "").strip()
                for item in relation.get("participants") or []
            ]
            relation_id = relation_mappings.get(
                relation_mapping_key(question_id, relation["local_name"], participant_ids),
                "",
            )
            if not relation_id:
                continue
            for attr in relation.get("attributes") or []:
                relation_attrs_by_owner.setdefault(relation_id, []).append(
                    {
                        "question_id": question_id,
                        "relation_local_name": relation["local_name"],
                        "participant_entity_ids": participant_ids,
                        **attr,
                    }
                )

        owner_payloads: list[dict[str, Any]] = []
        for entity in entity_payload.get("canonical_entities") or []:
            if not isinstance(entity, dict):
                continue
            owner_id = str(entity.get("entity_id") or "").strip()
            if not owner_id:
                continue
            owner_payloads.append(
                {
                    "stage": "attribute_alignment",
                    "owner_type": "entity",
                    "owner_id": owner_id,
                    "owner": entity,
                    "existing_attributes": existing_attrs_by_owner.get(("entity", owner_id), []),
                    "local_attributes": entity_attrs_by_owner.get(owner_id, []),
                }
            )

        for relation in relation_payload.get("canonical_relations") or []:
            if not isinstance(relation, dict):
                continue
            owner_id = str(relation.get("relation_id") or "").strip()
            if not owner_id:
                continue
            owner_payloads.append(
                {
                    "stage": "attribute_alignment",
                    "owner_type": "relation",
                    "owner_id": owner_id,
                    "owner": relation,
                    "existing_attributes": existing_attrs_by_owner.get(("relation", owner_id), []),
                    "local_attributes": relation_attrs_by_owner.get(owner_id, []),
                }
            )
        return owner_payloads

    def align_attributes(
        self,
        cases: list[dict[str, Any]],
        entity_payload: dict[str, Any],
        relation_payload: dict[str, Any],
        rewritten_relations: list[dict[str, Any]],
        alignment_attribute_payload: dict[str, Any],
    ) -> dict[str, Any]:
        owner_payloads = self.build_attribute_owner_payloads(
            cases,
            entity_payload,
            relation_payload,
            rewritten_relations,
            alignment_attribute_payload,
        )
        if not owner_payloads:
            return {"canonical_attributes": [], "attribute_mappings": []}
        outputs = self._call_json_batch("attribute_alignment", owner_payloads)
        attributes: list[dict[str, Any]] = []
        mappings: list[dict[str, Any]] = []
        for output in outputs:
            attributes.extend(list(output.get("canonical_attributes") or []))
            mappings.extend(list(output.get("attribute_mappings") or []))
        return {
            "canonical_attributes": dedupe_by_id(attributes, "attribute_id"),
            "attribute_mappings": mappings,
        }

    def cluster_topics(
        self,
        cases: list[dict[str, Any]],
        global_er: dict[str, Any],
    ) -> list[dict[str, Any]]:
        payload = self._call_json(
            "topic_clustering",
            {
                "stage": "topic_clustering",
                "questions": self._question_context(cases),
                "global_er": global_er,
                "question_subgraphs": global_er.get("question_mappings") or [],
            },
        )
        topics = payload.get("topics")
        if not isinstance(topics, list):
            raise ValueError("Topic clustering LLM response must contain `topics` list.")
        return [item for item in topics if isinstance(item, dict)]

    def build_global_er(
        self,
        cases: list[dict[str, Any]],
        entity_payload: dict[str, Any],
        relation_payload: dict[str, Any],
        attribute_payload: dict[str, Any],
    ) -> dict[str, Any]:
        raw_entities = dedupe_by_id(list(entity_payload.get("canonical_entities") or []), "entity_id")
        raw_relations = dedupe_by_id(list(relation_payload.get("canonical_relations") or []), "relation_id")
        raw_attributes = dedupe_by_id(
            list(attribute_payload.get("canonical_attributes") or []),
            "attribute_id",
        )
        entities = [strip_trace_fields(item) for item in raw_entities]
        relations = [strip_trace_fields(item) for item in raw_relations]
        attributes = [strip_trace_fields(item) for item in raw_attributes]
        question_mappings = self._build_question_mappings(
            cases=cases,
            entity_payload=entity_payload,
            relation_payload=relation_payload,
            attribute_payload=attribute_payload,
        )
        return {
            "entities": entities,
            "relations": relations,
            "attributes": attributes,
            "question_mappings": question_mappings,
        }

    def run(
        self,
        *,
        metadata_path: str | Path,
        output_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        resolved_metadata_path = resolve_path(metadata_path)
        resolved_output_dir = (
            resolve_path(output_dir)
            if output_dir is not None
            else self._default_output_dir(resolved_metadata_path)
        )
        resolved_output_dir.mkdir(parents=True, exist_ok=True)

        cases = self.load_cases(resolved_metadata_path)
        entity_payload = self.align_entities(cases)
        relation_payload, rewritten_relations = self.align_relations(cases, entity_payload)
        alignment_attribute_payload = self.collect_alignment_attributes(
            entity_payload,
            relation_payload,
        )
        owner_attribute_payload = self.align_attributes(
            cases,
            entity_payload,
            relation_payload,
            rewritten_relations,
            alignment_attribute_payload,
        )
        attribute_payload = merge_attribute_payloads(
            alignment_attribute_payload,
            owner_attribute_payload,
        )
        global_er = self.build_global_er(
            cases,
            entity_payload,
            relation_payload,
            attribute_payload,
        )
        topics = self.cluster_topics(cases, global_er)

        global_er_path = resolved_output_dir / GLOBAL_ER_FILENAME
        topics_path = resolved_output_dir / TOPICS_FILENAME
        summary_path = resolved_output_dir / SUMMARY_FILENAME
        alignment_steps_path = resolved_output_dir / ALIGNMENT_STEPS_FILENAME
        write_json(global_er_path, global_er)
        write_json(topics_path, {"topics": topics})
        write_json(
            alignment_steps_path,
            {
                "entity_alignment": entity_payload,
                "relation_alignment": relation_payload,
                "alignment_attributes": alignment_attribute_payload,
                "attribute_alignment": owner_attribute_payload,
                "merged_attributes": attribute_payload,
            },
        )
        summary = {
            "ok": True,
            "metadata_path": str(resolved_metadata_path),
            "output_dir": str(resolved_output_dir),
            "case_count": len(cases),
            "entity_count": len(global_er["entities"]),
            "relation_count": len(global_er["relations"]),
            "attribute_count": len(global_er["attributes"]),
            "topic_count": len(topics),
            "outputs": {
                "global_er": str(global_er_path),
                "topics": str(topics_path),
                "alignment_steps": str(alignment_steps_path),
            },
            "elapsed_seconds": round(time.perf_counter() - started_at, 3),
        }
        if self.log_dir is not None:
            summary["outputs"]["llm_log_dir"] = str(self.log_dir)
        write_json(summary_path, summary)
        return {
            "global_er": global_er,
            "topics": topics,
            "summary": summary,
        }

    @staticmethod
    def _default_output_dir(metadata_path: Path) -> Path:
        if metadata_path.is_file():
            return metadata_path.parent / DEFAULT_OUTPUT_DIR_NAME
        return metadata_path / DEFAULT_OUTPUT_DIR_NAME

    @staticmethod
    def _question_context(cases: list[dict[str, Any]]) -> list[dict[str, str]]:
        return [
            {
                "question_id": str(case.get("question_id") or ""),
                "question": str(case.get("question") or ""),
                "db_id": str(case.get("db_id") or ""),
            }
            for case in cases
        ]

    @staticmethod
    def _entity_mapping_index(entity_payload: dict[str, Any]) -> dict[tuple[str, str], str]:
        output: dict[tuple[str, str], str] = {}
        for item in BuildTopicRunner._entity_mapping_items(entity_payload):
            if not isinstance(item, dict):
                continue
            question_id = first_string(item, "question_id", "qid", "source_question_id")
            local_name = first_string(
                item,
                "local_name",
                "local_entity",
                "local_entity_name",
                "source_name",
                "name",
            )
            entity_id = first_string(
                item,
                "entity_id",
                "canonical_entity_id",
                "target_entity_id",
                "canonical_id",
            )
            if question_id and local_name and entity_id:
                output[mapping_key(question_id, local_name)] = entity_id
        return output

    @staticmethod
    def _local_ref_items(item: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for key in keys:
            value = item.get(key)
            if not isinstance(value, list):
                continue
            for ref in value:
                if isinstance(ref, dict):
                    refs.append(ref)
        return refs

    @staticmethod
    def _entity_mapping_items(entity_payload: dict[str, Any]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = [
            item
            for item in (
                entity_payload.get("entity_mappings")
                or entity_payload.get("mappings")
                or entity_payload.get("entity_alignment")
                or []
            )
            if isinstance(item, dict)
        ]
        for entity in entity_payload.get("canonical_entities") or []:
            if not isinstance(entity, dict):
                continue
            entity_id = first_string(
                entity,
                "entity_id",
                "canonical_entity_id",
                "target_entity_id",
                "canonical_id",
            )
            if not entity_id:
                continue
            refs = BuildTopicRunner._local_ref_items(
                entity,
                "local_refs",
                "local_names",
                "local_entities",
            )
            for ref in refs:
                item = dict(ref)
                item.setdefault("entity_id", entity_id)
                output.append(item)
            local_name = first_string(entity, "local_name", "source_name")
            question_id = first_string(entity, "question_id", "qid", "source_question_id")
            if local_name and question_id:
                output.append(
                    {
                        "question_id": question_id,
                        "local_name": local_name,
                        "entity_id": entity_id,
                    }
                )
        for attr in entity_payload.get("canonical_attributes") or []:
            if not isinstance(attr, dict):
                continue
            if first_string(attr, "owner_type") != "entity":
                continue
            owner_id = first_string(attr, "owner_id")
            if not owner_id:
                continue
            refs = BuildTopicRunner._local_ref_items(
                attr,
                "local_refs",
                "local_names",
                "local_attributes",
            )
            for ref in refs:
                local_name = first_string(
                    ref,
                    "local_name",
                    "local_entity",
                    "local_entity_name",
                    "source_name",
                    "name",
                )
                question_id = first_string(ref, "question_id", "qid", "source_question_id")
                if question_id and local_name:
                    output.append(
                        {
                            "question_id": question_id,
                            "local_name": local_name,
                            "entity_id": owner_id,
                        }
                    )
        return output

    @staticmethod
    def _relation_mapping_index(relation_payload: dict[str, Any]) -> dict[tuple[str, str, tuple[str, ...]], str]:
        output: dict[tuple[str, str, tuple[str, ...]], str] = {}
        for item in BuildTopicRunner._relation_mapping_items(relation_payload):
            if not isinstance(item, dict):
                continue
            question_id = first_string(item, "question_id", "qid", "source_question_id")
            local_name = first_string(
                item,
                "local_name",
                "local_relation",
                "local_relation_name",
                "source_name",
                "name",
            )
            participant_ids = unique_strings(
                list_from_keys(
                    item,
                    "participant_entity_ids",
                    "canonical_participant_entity_ids",
                    "entity_ids",
                )
            )
            relation_id = first_string(
                item,
                "relation_id",
                "canonical_relation_id",
                "target_relation_id",
                "canonical_id",
            )
            if question_id and relation_id:
                output[relation_mapping_key(question_id, local_name, participant_ids)] = relation_id
        return output

    @staticmethod
    def _relation_mapping_items(relation_payload: dict[str, Any]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = [
            item
            for item in (
                relation_payload.get("relation_mappings")
                or relation_payload.get("mappings")
                or relation_payload.get("relation_alignment")
                or []
            )
            if isinstance(item, dict)
        ]
        for relation in relation_payload.get("canonical_relations") or []:
            if not isinstance(relation, dict):
                continue
            relation_id = first_string(
                relation,
                "relation_id",
                "canonical_relation_id",
                "target_relation_id",
                "canonical_id",
            )
            if not relation_id:
                continue
            refs = BuildTopicRunner._local_ref_items(
                relation,
                "local_refs",
                "local_names",
                "local_relations",
            )
            for ref in refs:
                item = dict(ref)
                item.setdefault("relation_id", relation_id)
                output.append(item)
            local_name = first_string(relation, "local_name", "source_name")
            question_id = first_string(relation, "question_id", "qid", "source_question_id")
            if local_name and question_id:
                output.append(
                    {
                        "question_id": question_id,
                        "local_name": local_name,
                        "relation_id": relation_id,
                    }
                )
        return output

    @staticmethod
    def _attribute_mapping_items(attribute_payload: dict[str, Any]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = [
            item
            for item in attribute_payload.get("attribute_mappings") or []
            if isinstance(item, dict)
        ]
        for attr in attribute_payload.get("canonical_attributes") or []:
            if not isinstance(attr, dict):
                continue
            attribute_id = first_string(
                attr,
                "attribute_id",
                "canonical_attribute_id",
                "target_attribute_id",
                "canonical_id",
            )
            owner_type = first_string(attr, "owner_type")
            owner_id = first_string(attr, "owner_id")
            if not attribute_id:
                continue
            refs = BuildTopicRunner._local_ref_items(
                attr,
                "local_refs",
                "local_names",
                "local_attributes",
            )
            for ref in refs:
                item = dict(ref)
                item.setdefault("attribute_id", attribute_id)
                if owner_type:
                    item.setdefault("owner_type", owner_type)
                if owner_id:
                    item.setdefault("owner_id", owner_id)
                output.append(item)
        return output

    def _build_question_mappings(
        self,
        *,
        cases: list[dict[str, Any]],
        entity_payload: dict[str, Any],
        relation_payload: dict[str, Any],
        attribute_payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        entity_mappings = self._entity_mapping_index(entity_payload)
        relation_mappings_by_question: dict[str, list[dict[str, Any]]] = {}
        for item in self._relation_mapping_items(relation_payload):
            if not isinstance(item, dict):
                continue
            question_id = first_string(item, "question_id", "qid", "source_question_id")
            if question_id:
                relation_mappings_by_question.setdefault(question_id, []).append(item)

        attribute_mappings_by_question: dict[str, list[dict[str, Any]]] = {}
        for item in self._attribute_mapping_items(attribute_payload):
            if not isinstance(item, dict):
                continue
            question_id = str(item.get("question_id") or "").strip()
            if question_id:
                attribute_mappings_by_question.setdefault(question_id, []).append(item)

        mappings: list[dict[str, Any]] = []
        for case in cases:
            question_id = str(case["question_id"])
            entity_ids = unique_strings(
                [
                    entity_mappings.get(mapping_key(question_id, entity["local_name"]), "")
                    for entity in case["entities"]
                ]
            )
            relation_ids = unique_strings(
                [
                    first_string(
                        item,
                        "relation_id",
                        "canonical_relation_id",
                        "target_relation_id",
                        "canonical_id",
                    )
                    for item in relation_mappings_by_question.get(question_id, [])
                ]
            )
            attribute_ids = unique_strings(
                [
                    item.get("attribute_id") or item.get("canonical_attribute_id") or ""
                    for item in attribute_mappings_by_question.get(question_id, [])
                ]
            )
            local_to_global = {
                "entities": {
                    entity["local_name"]: entity_mappings.get(
                        mapping_key(question_id, entity["local_name"]),
                        "",
                    )
                    for entity in case["entities"]
                },
                "relations": {
                    first_string(
                        item,
                        "local_name",
                        "local_relation",
                        "local_relation_name",
                        "source_name",
                        "name",
                    ): first_string(
                        item,
                        "relation_id",
                        "canonical_relation_id",
                        "target_relation_id",
                        "canonical_id",
                    )
                    for item in relation_mappings_by_question.get(question_id, [])
                },
                "attributes": {
                    f"{item.get('owner_type', '')}:{item.get('owner_id', '')}:{item.get('local_name', '')}": str(
                        item.get("attribute_id") or item.get("canonical_attribute_id") or ""
                    )
                    for item in attribute_mappings_by_question.get(question_id, [])
                },
            }
            mappings.append(
                {
                    "question_id": question_id,
                    "question": str(case.get("question") or ""),
                    "entity_ids": entity_ids,
                    "relation_ids": relation_ids,
                    "attribute_ids": attribute_ids,
                    "local_to_global": local_to_global,
                }
            )
        return mappings


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build global ER and topics from metadata_path/**/nl2er.json."
    )
    parser.add_argument("--metadata-path", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--prompt-dir", type=Path, default=DEFAULT_PROMPT_DIR)
    parser.add_argument("--model-config", default=None)
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument(
        "--reasoning-mode",
        choices=sorted(REASONING_MODE_MAP.keys()),
        default=None,
    )
    parser.add_argument("--max-attribute-workers", type=int, default=8)
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    run_name = build_timestamp()
    resolved_metadata_path = resolve_path(args.metadata_path)
    resolved_output_dir = (
        resolve_path(args.output_dir)
        if args.output_dir is not None
        else BuildTopicRunner._default_output_dir(resolved_metadata_path)
    )
    resolved_log_dir = (
        resolve_path(args.log_dir)
        if args.log_dir is not None
        else resolve_path(args.log_root) / run_name
    )
    resolved_log_dir.mkdir(parents=True, exist_ok=True)
    print(f"[BUILDTOPIC] run={run_name}")
    print(f"[BUILDTOPIC] metadata_path={resolved_metadata_path}")
    print(f"[BUILDTOPIC] log_dir={resolved_log_dir}")
    runner = BuildTopicRunner(
        model_config=args.model_config,
        reasoning_mode=args.reasoning_mode,
        prompt_dir=args.prompt_dir,
        max_attribute_workers=args.max_attribute_workers,
        log_dir=resolved_log_dir,
    )
    result = runner.run(metadata_path=resolved_metadata_path, output_dir=resolved_output_dir)
    print(
        "[BUILDTOPIC] completed "
        f"cases={result['summary']['case_count']} "
        f"entities={result['summary']['entity_count']} "
        f"relations={result['summary']['relation_count']} "
        f"attributes={result['summary']['attribute_count']} "
        f"topics={result['summary']['topic_count']} "
        f"output_dir={result['summary']['output_dir']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
