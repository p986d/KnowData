from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ColumnSampleStats:
    sample_row_count: int
    observed_count: int
    non_null_count: int
    null_count: int
    distinct_count: int
    null_ratio: float
    high_frequency_values: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ColumnSnapshotEvidence:
    column_fullname: str
    column_name: str
    data_type: str
    description: str
    sample_values: list[Any] = field(default_factory=list)
    sample_stats: ColumnSampleStats | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DataSnapshotRelationClue:
    source_table: str
    source_column: str
    target_table: str
    target_column: str
    clue_type: str
    confidence: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DataSnapshotEvidence:
    snapshot_id: str
    source_kind: str
    table_fullname: str
    namespace: str
    table_name: str
    snapshot_path: str
    table_description: str = ""
    group_id: str = ""
    grouping_method: str = ""
    member_tables: list[str] = field(default_factory=list)
    columns: list[ColumnSnapshotEvidence] = field(default_factory=list)
    sample_rows: list[dict[str, Any]] = field(default_factory=list)
    name_evidence: dict[str, Any] = field(default_factory=dict)
    structure_evidence: dict[str, Any] = field(default_factory=dict)
    content_evidence: dict[str, Any] = field(default_factory=dict)
    statistic_evidence: dict[str, Any] = field(default_factory=dict)
    explicit_constraints: dict[str, Any] = field(default_factory=dict)
    context_evidence: dict[str, Any] = field(default_factory=dict)
    relation_clues: list[DataSnapshotRelationClue] = field(default_factory=list)
    quality_warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DataUnitShapeJudgment:
    shape_type: str
    description: str = ""
    confidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DataUnitGrainJudgment:
    grain_kind: str = ""
    grain_name: str = ""
    definition: str = ""
    identifier_sets: list[dict[str, Any]] = field(default_factory=list)
    entity: dict[str, Any] = field(default_factory=dict)
    relationship: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DataUnitShapeGrainAnalysis:
    snapshot_id: str
    table_fullname: str
    source_kind: str
    shape: DataUnitShapeJudgment
    grain: DataUnitGrainJudgment
    analysis_notes: list[str] = field(default_factory=list)
    prompt_path: str = ""
    response_path: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

