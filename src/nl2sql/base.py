from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(slots=True)
class EngineRuntimeConfig:
    provider_name: str
    root: Path | None = None
    spider2_root: Path | None = None
    schema_linking_script: Path | None = None
    nl2sql_script: Path | None = None
    database_root: Path | None = None
    database_source: str | None = None
    python_executable: str = sys.executable or "python"


@dataclass(slots=True)
class SchemaLinkingRequest:
    api_key: str
    base_url: str
    db_id: str
    question: str
    run_prefix: str | None = None
    model: str = "qwen-plus"
    output_path: str | None = None
    temperature: float = 0.0
    shortlist_trigger: int = 18
    max_shortlist_tables: int = 24
    sample_row_limit: int = 2
    sample_value_max_chars: int = 300
    similar_tables_hint_limit: int = 12
    timeout_seconds: int | None = None


@dataclass(slots=True)
class NL2SQLRequest:
    question: str
    db_id: str
    external_knowledge: str = ""
    run_prefix: str | None = None
    llm_config_name: str | None = None
    output_path: str | None = None
    temperature: float = 0.7
    schema_link_temperature: float = 0.0
    num_votes: int = 4
    max_workers: int = 4
    max_iter: int = 5
    timeout_seconds: float = 600.0
    generation_model: str | None = None
    column_exploration_model: str | None = None
    vote_model: str | None = None


@dataclass(slots=True)
class SchemaLinkingResult:
    ok: bool
    engine: str
    result_path: str | None
    temporary_output: bool
    stdout: str
    stderr: str
    exit_code: int | None
    error: str | None = None
    raw_result: dict[str, Any] | None = None
    tables: list[dict[str, str]] = field(default_factory=list)
    columns: list[dict[str, str]] = field(default_factory=list)
    warning: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "engine": self.engine,
            "result_path": self.result_path,
            "temporary_output": self.temporary_output,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "error": self.error,
            "result": self.raw_result,
            "raw_result": self.raw_result,
            "tables": self.tables,
            "columns": self.columns,
        }
        if self.warning:
            payload["warning"] = self.warning
        return payload


@dataclass(slots=True)
class NL2SQLResult:
    ok: bool
    engine: str
    result_path: str | None
    temporary_output: bool
    stdout: str
    stderr: str
    exit_code: int | None
    sql: str = ""
    error: str | None = None
    raw_result: dict[str, Any] | None = None
    tables: list[dict[str, str]] = field(default_factory=list)
    columns: list[dict[str, str]] = field(default_factory=list)
    schema_linking_result_path: str | None = None
    warning: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "engine": self.engine,
            "result_path": self.result_path,
            "temporary_output": self.temporary_output,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "sql": self.sql,
            "error": self.error,
            "result": self.raw_result,
            "raw_result": self.raw_result,
            "tables": self.tables,
            "columns": self.columns,
            "schema_linking_result_path": self.schema_linking_result_path,
        }
        if self.warning:
            payload["warning"] = self.warning
        return payload


class EngineProvider(Protocol):
    def run_schema_linking(
        self,
        request: SchemaLinkingRequest,
        *,
        runtime: EngineRuntimeConfig | None = None,
        raise_on_error: bool = False,
    ) -> SchemaLinkingResult:
        ...

    def run_nl2sql(
        self,
        request: NL2SQLRequest,
        *,
        runtime: EngineRuntimeConfig | None = None,
        raise_on_error: bool = False,
    ) -> NL2SQLResult:
        ...
