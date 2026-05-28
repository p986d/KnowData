from __future__ import annotations

from pathlib import Path
from typing import Any

from src.config import load_settings
from src.config.schema import LLMConfig
from src.dataconcept.models import DataSnapshotEvidence, DataUnitShapeGrainAnalysis
from src.llm.llm_client import LLMClient
from src.prompt.prompt_builder import PromptBuilder


DEFAULT_PROMPT_DIR = Path("src/prompt/prompt_template")
CLUSTER_TEMPLATE_KEY = "dataconcept_concept_clustering"
CLUSTER_TEMPLATE_NAME = "DataConcept_concept_clustering_v0.1.md"
CONNECTION_TEMPLATE_KEY = "dataconcept_concept_connection"
CONNECTION_TEMPLATE_NAME = "DataConcept_concept_connection_v0.1.md"


def unique_strings(values: Any) -> list[str]:
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


def first_string(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def concept_name_from_analysis(analysis: DataUnitShapeGrainAnalysis) -> str:
    grain = analysis.grain
    if grain.grain_kind == "entity":
        return first_string(grain.entity.get("entity_name"), grain.grain_name, "unknown")
    if grain.grain_kind == "relationship":
        return first_string(grain.relationship.get("relationship_name"), grain.grain_name, "unknown")
    return first_string(grain.grain_name, grain.grain_kind, "unknown")


def collect_representative_columns(analysis: DataUnitShapeGrainAnalysis) -> list[str]:
    fields: list[str] = []
    for identifier_set in analysis.grain.identifier_sets:
        if isinstance(identifier_set, dict):
            fields.extend(unique_strings(identifier_set.get("columns")))
    fields.extend(unique_strings(analysis.grain.entity.get("representative_columns")))
    fields.extend(unique_strings(analysis.grain.relationship.get("representative_columns")))
    for participant in analysis.grain.relationship.get("participant_entities") or []:
        if isinstance(participant, dict):
            fields.extend(unique_strings(participant.get("identifier_columns")))
    return unique_strings(fields)


def build_concept_index(analyses: list[DataUnitShapeGrainAnalysis]) -> list[dict[str, Any]]:
    index: list[dict[str, Any]] = []
    for analysis in analyses:
        if analysis.error:
            continue
        concept_ref = f"unit:{analysis.table_fullname}"
        index.append(
            {
                "concept_ref": concept_ref,
                "table_fullname": analysis.table_fullname,
                "snapshot_id": analysis.snapshot_id,
                "shape_type": analysis.shape.shape_type,
                "grain_kind": analysis.grain.grain_kind,
                "grain_name": analysis.grain.grain_name,
                "concept_name": concept_name_from_analysis(analysis),
                "definition": analysis.grain.definition,
                "identifier_sets": analysis.grain.identifier_sets,
                "entity": analysis.grain.entity,
                "relationship": analysis.grain.relationship,
                "representative_columns": collect_representative_columns(analysis),
            }
        )
    return index


def compact_snapshot_for_connection(snapshot: DataSnapshotEvidence) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "table_fullname": snapshot.table_fullname,
        "table_name": snapshot.table_name,
        "table_description": snapshot.table_description,
        "columns": [
            {
                "column_fullname": column.column_fullname,
                "column_name": column.column_name,
                "data_type": column.data_type,
                "description": column.description,
                "sample_values": column.sample_values,
                "sample_stats": column.sample_stats.to_dict() if column.sample_stats else {},
            }
            for column in snapshot.columns
        ],
        "sample_rows": snapshot.sample_rows,
        "provided_constraints": snapshot.explicit_constraints,
        "relation_clues": [clue.to_dict() for clue in snapshot.relation_clues],
    }


def _column_lookup(snapshots: list[DataSnapshotEvidence]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for snapshot in snapshots:
        for column in snapshot.columns:
            if column.column_fullname:
                lookup[column.column_fullname.casefold()] = column.column_fullname
            if column.column_name and column.column_fullname:
                lookup[f"{snapshot.table_fullname.casefold()}.{column.column_name.casefold()}"] = column.column_fullname
    return lookup


def resolve_columns_for_table(values: Any, *, table_fullname: str, lookup: dict[str, str]) -> list[str]:
    resolved: list[str] = []
    seen: set[str] = set()
    for value in unique_strings(values):
        key = value.casefold()
        full_key = f"{table_fullname.casefold()}.{key}"
        resolved_value = lookup.get(key) or lookup.get(full_key) or value
        if resolved_value in seen:
            continue
        resolved.append(resolved_value)
        seen.add(resolved_value)
    return resolved


def _concept_by_ref(concept_index: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("concept_ref") or ""): item
        for item in concept_index
        if str(item.get("concept_ref") or "").strip()
    }


def extract_cluster_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    for key in ("database_concept_clustering", "concept_clustering", "result"):
        value = parsed.get(key)
        if isinstance(value, dict):
            return value
    return parsed


def normalize_cluster_payload(
    *,
    parsed: dict[str, Any],
    concept_index: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    payload = extract_cluster_payload(parsed)
    by_ref = _concept_by_ref(concept_index)
    clusters: list[dict[str, Any]] = []
    for index, cluster in enumerate(payload.get("concept_clusters") or payload.get("clusters") or []):
        if not isinstance(cluster, dict):
            continue
        members: list[dict[str, Any]] = []
        for member in cluster.get("members") or []:
            if isinstance(member, str):
                concept_ref = member
                source = by_ref.get(concept_ref, {})
                members.append(
                    {
                        "concept_ref": concept_ref,
                        "table_fullname": source.get("table_fullname", ""),
                        "grain_name": source.get("grain_name", ""),
                        "grain_kind": source.get("grain_kind", ""),
                        "shape_type": source.get("shape_type", ""),
                    }
                )
                continue
            if isinstance(member, dict):
                concept_ref = first_string(member.get("concept_ref"), member.get("id"), member.get("table_fullname"))
                source = by_ref.get(concept_ref, {})
                members.append(
                    {
                        "concept_ref": concept_ref,
                        "table_fullname": first_string(member.get("table_fullname"), source.get("table_fullname")),
                        "grain_name": first_string(member.get("grain_name"), source.get("grain_name")),
                        "grain_kind": first_string(member.get("grain_kind"), source.get("grain_kind")),
                        "shape_type": first_string(member.get("shape_type"), source.get("shape_type")),
                    }
                )
        clusters.append(
            {
                "cluster_id": first_string(cluster.get("cluster_id"), cluster.get("id"), f"cluster_{index + 1}"),
                "cluster_label": first_string(cluster.get("cluster_label"), cluster.get("label"), cluster.get("name")),
                "members": members,
                "cluster_reason": first_string(cluster.get("cluster_reason"), cluster.get("reason"), cluster.get("description")),
            }
        )
    return clusters


def extract_connection_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    for key in ("source_concept_connections", "concept_connections", "connection_profile", "result"):
        value = parsed.get(key)
        if isinstance(value, dict):
            return value
    return parsed


def normalize_connection_payload(
    *,
    parsed: dict[str, Any],
    source_snapshot: DataSnapshotEvidence,
    source_analysis: DataUnitShapeGrainAnalysis,
    concept_index: list[dict[str, Any]],
    snapshots: list[DataSnapshotEvidence],
) -> dict[str, Any]:
    payload = extract_connection_payload(parsed)
    by_ref = _concept_by_ref(concept_index)
    by_table = {
        str(item.get("table_fullname") or ""): item
        for item in concept_index
        if str(item.get("table_fullname") or "").strip()
    }
    lookup = _column_lookup(snapshots)
    source_concept_ref = first_string(payload.get("source_concept_ref"), f"unit:{source_analysis.table_fullname}")
    connections: list[dict[str, Any]] = []
    for connection in payload.get("connections") or []:
        if not isinstance(connection, dict):
            continue
        target_ref = first_string(connection.get("target_concept_ref"), connection.get("target_ref"))
        target_table = first_string(connection.get("target_table"), connection.get("target_table_fullname"))
        if not target_ref and target_table:
            target_ref = str(by_table.get(target_table, {}).get("concept_ref") or "")
        target = by_ref.get(target_ref, by_table.get(target_table, {}))
        target_table = first_string(target_table, target.get("table_fullname"))
        source_columns = resolve_columns_for_table(
            connection.get("source_columns"),
            table_fullname=source_snapshot.table_fullname,
            lookup=lookup,
        )
        target_identifier_columns = resolve_columns_for_table(
            connection.get("target_identifier_columns"),
            table_fullname=target_table,
            lookup=lookup,
        )
        if target_ref == source_concept_ref and set(source_columns) & set(target_identifier_columns):
            continue
        connections.append(
            {
                "source_columns": source_columns,
                "target_concept_ref": target_ref,
                "target_table": target_table,
                "target_concept_name": first_string(connection.get("target_concept_name"), target.get("concept_name")),
                "target_identifier_columns": target_identifier_columns,
                "connection_type": first_string(connection.get("connection_type"), "possible_reference"),
                "confidence": first_string(connection.get("confidence"), "low"),
                "definition": first_string(connection.get("definition"), connection.get("basis"), connection.get("reason")),
            }
        )
    return {
        "source_table": source_snapshot.table_fullname,
        "source_concept_ref": source_concept_ref,
        "source_concept_name": concept_name_from_analysis(source_analysis),
        "connections": connections,
        "analysis_notes": unique_strings(payload.get("analysis_notes") or payload.get("notes"))[:3],
    }


def build_empty_connection_profile(
    *,
    source_snapshot: DataSnapshotEvidence,
    source_analysis: DataUnitShapeGrainAnalysis,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "source_table": source_snapshot.table_fullname,
        "source_concept_ref": f"unit:{source_analysis.table_fullname}",
        "source_concept_name": concept_name_from_analysis(source_analysis),
        "connections": [],
        "analysis_notes": unique_strings([reason]) if reason else [],
    }


class DatabaseConceptCatalogAnalyzer:
    def __init__(
        self,
        *,
        prompt_dir: str | Path = DEFAULT_PROMPT_DIR,
        model_config: str | None = None,
        llm_config: LLMConfig | None = None,
        dry_run: bool = False,
        llm: LLMClient | None = None,
    ) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.model_config = model_config
        self.dry_run = dry_run
        self.llm = llm
        if not self.dry_run and self.llm is None:
            config = llm_config
            if config is None:
                settings = load_settings()
                config = settings.llm.get(model_config)
            self.llm = LLMClient(config)
        self.prompt_builder = PromptBuilder(template_dir=self.prompt_dir, strict_undefined=True)
        self.prompt_builder.register_template(
            name=CLUSTER_TEMPLATE_KEY,
            template_name=CLUSTER_TEMPLATE_NAME,
            required_vars=["concept_index"],
        )
        self.prompt_builder.register_template(
            name=CONNECTION_TEMPLATE_KEY,
            template_name=CONNECTION_TEMPLATE_NAME,
            required_vars=["source_snapshot", "source_concept", "concept_index"],
        )

    def build_clustering_prompt(self, concept_index: list[dict[str, Any]]) -> str:
        return self.prompt_builder.build_text(
            CLUSTER_TEMPLATE_KEY,
            vars={"concept_index": concept_index},
        )

    def build_connection_prompt(
        self,
        *,
        source_snapshot: DataSnapshotEvidence,
        source_analysis: DataUnitShapeGrainAnalysis,
        concept_index: list[dict[str, Any]],
    ) -> str:
        return self.prompt_builder.build_text(
            CONNECTION_TEMPLATE_KEY,
            vars={
                "source_snapshot": compact_snapshot_for_connection(source_snapshot),
                "source_concept": {
                    "concept_ref": f"unit:{source_analysis.table_fullname}",
                    "table_fullname": source_analysis.table_fullname,
                    "shape_type": source_analysis.shape.shape_type,
                    "grain": source_analysis.grain.to_dict(),
                },
                "concept_index": concept_index,
            },
        )

    def build_catalog_payload(
        self,
        *,
        db_id: str,
        snapshots: list[DataSnapshotEvidence],
        analyses: list[DataUnitShapeGrainAnalysis],
        concept_clusters: list[dict[str, Any]],
        source_connection_profiles: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "db_id": db_id,
            "concept_index": build_concept_index(analyses),
            "concept_clusters": concept_clusters,
            "source_connection_profiles": source_connection_profiles,
        }
