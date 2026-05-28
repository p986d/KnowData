from __future__ import annotations

import argparse
import time
import traceback
from pathlib import Path
from typing import Any

from src.config import load_settings
from src.dataconcept.catalog import (
    DatabaseConceptCatalogAnalyzer,
    build_concept_index,
    build_empty_connection_profile,
    normalize_cluster_payload,
    normalize_connection_payload,
)
from src.dataconcept.evidence import build_data_snapshots_from_tables
from src.dataconcept.shape_grain import (
    DEFAULT_PROMPT_DIR,
    ShapeGrainAnalyzer,
    build_unknown_shape_grain_analysis,
    normalize_shape_grain_analysis,
)
from src.er2data.physical_schema import load_database_tables
from src.er2data.table_grouping import normalize_casefold_set
from src.llm.llm_client import LLMClient
from src.llm.reasoning import REASONING_MODE_MAP, apply_reasoning_mode
from src.utils.json_util import json_check, json_parse
from src.utils.run_log import build_timestamp, emit_step_done_log, write_json


DEFAULT_LOG_ROOT = Path("log/database_data_concept")
DEFAULT_OUTPUT_FILENAME = "database_data_concept.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build database data-concept snapshot evidence and LLM shape/grain analysis."
    )
    parser.add_argument("--db-id", required=True)
    parser.add_argument("--database-root", type=Path, default=Path("databases"))
    parser.add_argument("--spider2-root", type=Path, default=None)
    parser.add_argument("--output-path", type=Path, default=None)
    parser.add_argument("--output-filename", default=DEFAULT_OUTPUT_FILENAME)
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--timestamp", default=None)
    parser.add_argument("--prompt-dir", type=Path, default=DEFAULT_PROMPT_DIR)
    parser.add_argument("--model-config", default=None)
    parser.add_argument(
        "--reasoning-mode",
        choices=sorted(REASONING_MODE_MAP.keys()),
        default=None,
    )
    parser.add_argument(
        "--table-fullname",
        nargs="+",
        default=None,
        help="Optional table fullname filters. Supports comma-separated values.",
    )
    parser.add_argument("--sample-values-per-column", type=int, default=5)
    parser.add_argument("--max-concurrency", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def selected_table_names(values: list[str] | None) -> set[str]:
    return normalize_casefold_set(values)


def filter_tables_by_fullname(tables: list[Any], filters: set[str]) -> list[Any]:
    if not filters:
        return tables
    return [table for table in tables if table.full_name.casefold() in filters]


def safe_snapshot_stem(snapshot_id: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in snapshot_id).strip("._") or "snapshot"


class DatabaseDataConceptRunner:
    def __init__(
        self,
        *,
        db_id: str,
        database_root: str | Path | None,
        spider2_root: str | Path | None,
        log_dir: str | Path,
        prompt_dir: str | Path,
        model_config: str | None = None,
        reasoning_mode: str | None = None,
        sample_values_per_column: int = 5,
        dry_run: bool = False,
    ) -> None:
        self.db_id = db_id
        self.database_root = database_root
        self.spider2_root = spider2_root
        self.log_dir = Path(log_dir).resolve()
        self.prompt_dir = Path(prompt_dir)
        self.model_config = model_config
        self.reasoning_mode = reasoning_mode
        self.sample_values_per_column = sample_values_per_column
        self.dry_run = dry_run
        self.prompt_log_dir = self.log_dir / "shape_grain_prompts"
        self.response_log_dir = self.log_dir / "shape_grain_responses"
        self.catalog_prompt_log_dir = self.log_dir / "catalog_prompts"
        self.catalog_response_log_dir = self.log_dir / "catalog_responses"
        self.connection_prompt_log_dir = self.catalog_prompt_log_dir / "connections"
        self.connection_response_log_dir = self.catalog_response_log_dir / "connections"
        for directory in (
            self.log_dir,
            self.prompt_log_dir,
            self.response_log_dir,
            self.catalog_prompt_log_dir,
            self.catalog_response_log_dir,
            self.connection_prompt_log_dir,
            self.connection_response_log_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        llm = None
        if not self.dry_run:
            settings = load_settings()
            config = apply_reasoning_mode(settings.llm.get(model_config), reasoning_mode)
            llm = LLMClient(config)
        self.shape_grain_analyzer = ShapeGrainAnalyzer(
            prompt_dir=self.prompt_dir,
            model_config=model_config,
            dry_run=self.dry_run,
            llm=llm,
        )
        self.catalog_analyzer = DatabaseConceptCatalogAnalyzer(
            prompt_dir=self.prompt_dir,
            model_config=model_config,
            dry_run=self.dry_run,
            llm=llm,
        )

    def build_snapshots(self, *, table_filters: set[str]) -> list[Any]:
        tables = load_database_tables(
            db_id=self.db_id,
            database_root=self.database_root,
            spider2_root=self.spider2_root,
        )
        tables = filter_tables_by_fullname(tables, table_filters)
        if not tables:
            raise ValueError("No tables were found for data concept analysis.")
        return build_data_snapshots_from_tables(
            tables,
            group_table_families=True,
            sample_values_per_column=self.sample_values_per_column,
        )

    def analyze_shape_grain(self, *, snapshots: list[Any], max_concurrency: int) -> list[Any]:
        prompts: list[str] = []
        prompt_paths: list[Path] = []
        response_paths: list[Path] = []
        for index, snapshot in enumerate(snapshots, start=1):
            stem = f"{index:04d}_{safe_snapshot_stem(snapshot.snapshot_id)}"
            prompt = self.shape_grain_analyzer.build_prompt(snapshot)
            prompt_path = self.prompt_log_dir / f"{stem}.md"
            response_path = self.response_log_dir / f"{stem}.json"
            prompt_path.write_text(prompt, encoding="utf-8")
            prompts.append(prompt)
            prompt_paths.append(prompt_path)
            response_paths.append(response_path)

        if self.dry_run:
            return [
                build_unknown_shape_grain_analysis(
                    snapshot=snapshot,
                    reason="Dry-run placeholder: LLM shape and grain analysis was not performed.",
                    prompt_path=str(prompt_path),
                    response_path=str(response_path),
                )
                for snapshot, prompt_path, response_path in zip(snapshots, prompt_paths, response_paths)
            ]

        if self.shape_grain_analyzer.llm is None:
            raise RuntimeError("LLM client is not initialized.")
        raw_responses = self.shape_grain_analyzer.llm.batch_single_turn(
            prompts,
            check_func=json_check,
            max_concurrency=max(1, int(max_concurrency or 1)),
            show_progress=len(prompts) > 1,
            progress_desc="Data concept shape/grain",
        )
        analyses: list[Any] = []
        for snapshot, prompt_path, response_path, raw_response in zip(
            snapshots,
            prompt_paths,
            response_paths,
            raw_responses,
        ):
            response_path.write_text(raw_response or "", encoding="utf-8")
            if not raw_response or not raw_response.strip():
                analyses.append(
                    build_unknown_shape_grain_analysis(
                        snapshot=snapshot,
                        reason="LLM returned empty response.",
                        prompt_path=str(prompt_path),
                        response_path=str(response_path),
                        error="empty_response",
                    )
                )
                continue
            try:
                parsed = json_parse(raw_response)
            except Exception as exc:
                analyses.append(
                    build_unknown_shape_grain_analysis(
                        snapshot=snapshot,
                        reason="Failed to parse LLM shape and grain analysis response.",
                        prompt_path=str(prompt_path),
                        response_path=str(response_path),
                        error=str(exc),
                    )
                )
                continue
            analyses.append(
                normalize_shape_grain_analysis(
                    parsed=parsed,
                    snapshot=snapshot,
                    prompt_path=str(prompt_path),
                    response_path=str(response_path),
                )
            )
        return analyses

    def analyze_catalog(
        self,
        *,
        snapshots: list[Any],
        analyses: list[Any],
        max_concurrency: int,
    ) -> dict[str, Any]:
        concept_index = build_concept_index(analyses)

        cluster_prompt = self.catalog_analyzer.build_clustering_prompt(concept_index)
        cluster_prompt_path = self.catalog_prompt_log_dir / "concept_clustering.md"
        cluster_response_path = self.catalog_response_log_dir / "concept_clustering.json"
        cluster_prompt_path.write_text(cluster_prompt, encoding="utf-8")

        if self.dry_run:
            concept_clusters: list[dict[str, Any]] = []
            cluster_response_path.write_text("", encoding="utf-8")
        else:
            if self.catalog_analyzer.llm is None:
                raise RuntimeError("LLM client is not initialized.")
            raw_cluster_response = self.catalog_analyzer.llm.single_turn(
                cluster_prompt,
                check_func=json_check,
            )
            cluster_response_path.write_text(raw_cluster_response or "", encoding="utf-8")
            if raw_cluster_response and raw_cluster_response.strip():
                concept_clusters = normalize_cluster_payload(
                    parsed=json_parse(raw_cluster_response),
                    concept_index=concept_index,
                )
            else:
                concept_clusters = []

        analysis_by_snapshot_id = {analysis.snapshot_id: analysis for analysis in analyses}
        connection_items: list[tuple[Any, Any, Path, Path]] = []
        connection_prompts: list[str] = []
        for index, snapshot in enumerate(snapshots, start=1):
            source_analysis = analysis_by_snapshot_id.get(snapshot.snapshot_id)
            if source_analysis is None:
                continue
            stem = f"{index:04d}_{safe_snapshot_stem(snapshot.snapshot_id)}"
            prompt = self.catalog_analyzer.build_connection_prompt(
                source_snapshot=snapshot,
                source_analysis=source_analysis,
                concept_index=concept_index,
            )
            prompt_path = self.connection_prompt_log_dir / f"{stem}.md"
            response_path = self.connection_response_log_dir / f"{stem}.json"
            prompt_path.write_text(prompt, encoding="utf-8")
            connection_prompts.append(prompt)
            connection_items.append((snapshot, source_analysis, prompt_path, response_path))

        source_connection_profiles: list[dict[str, Any]] = []
        if self.dry_run:
            for snapshot, source_analysis, _, response_path in connection_items:
                response_path.write_text("", encoding="utf-8")
                source_connection_profiles.append(
                    build_empty_connection_profile(
                        source_snapshot=snapshot,
                        source_analysis=source_analysis,
                        reason="Dry-run placeholder: LLM concept connection analysis was not performed.",
                    )
                )
        else:
            if self.catalog_analyzer.llm is None:
                raise RuntimeError("LLM client is not initialized.")
            raw_connection_responses = self.catalog_analyzer.llm.batch_single_turn(
                connection_prompts,
                check_func=json_check,
                max_concurrency=max(1, int(max_concurrency or 1)),
                show_progress=len(connection_prompts) > 1,
                progress_desc="Data concept connections",
            )
            for snapshot, source_analysis, _, response_path, raw_response in zip(
                [item[0] for item in connection_items],
                [item[1] for item in connection_items],
                [item[2] for item in connection_items],
                [item[3] for item in connection_items],
                raw_connection_responses,
            ):
                response_path.write_text(raw_response or "", encoding="utf-8")
                if not raw_response or not raw_response.strip():
                    source_connection_profiles.append(
                        build_empty_connection_profile(
                            source_snapshot=snapshot,
                            source_analysis=source_analysis,
                            reason="LLM returned empty concept connection response.",
                        )
                    )
                    continue
                try:
                    parsed = json_parse(raw_response)
                    source_connection_profiles.append(
                        normalize_connection_payload(
                            parsed=parsed,
                            source_snapshot=snapshot,
                            source_analysis=source_analysis,
                            concept_index=concept_index,
                            snapshots=snapshots,
                        )
                    )
                except Exception as exc:
                    source_connection_profiles.append(
                        build_empty_connection_profile(
                            source_snapshot=snapshot,
                            source_analysis=source_analysis,
                            reason=f"Failed to parse concept connection response: {exc}",
                        )
                    )

        return self.catalog_analyzer.build_catalog_payload(
            db_id=self.db_id,
            snapshots=snapshots,
            analyses=analyses,
            concept_clusters=concept_clusters,
            source_connection_profiles=source_connection_profiles,
        )

    def run(
        self,
        *,
        output_path: str | Path,
        table_filters: set[str],
        max_concurrency: int,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        snapshots = self.build_snapshots(table_filters=table_filters)
        emit_step_done_log(
            prefix="DATABASE_DATA_CONCEPT",
            step="snapshot_evidence",
            elapsed_seconds=time.perf_counter() - started_at,
            snapshot_count=len(snapshots),
        )

        shape_started_at = time.perf_counter()
        analyses = self.analyze_shape_grain(
            snapshots=snapshots,
            max_concurrency=max_concurrency,
        )
        emit_step_done_log(
            prefix="DATABASE_DATA_CONCEPT",
            step="shape_grain_analysis",
            elapsed_seconds=time.perf_counter() - shape_started_at,
            analysis_count=len(analyses),
            dry_run=bool(self.dry_run),
        )

        catalog_started_at = time.perf_counter()
        catalog = self.analyze_catalog(
            snapshots=snapshots,
            analyses=analyses,
            max_concurrency=max_concurrency,
        )
        emit_step_done_log(
            prefix="DATABASE_DATA_CONCEPT",
            step="database_concept_catalog",
            elapsed_seconds=time.perf_counter() - catalog_started_at,
            concept_count=len(catalog.get("concept_index") or []),
            cluster_count=len(catalog.get("concept_clusters") or []),
            connection_profile_count=len(catalog.get("source_connection_profiles") or []),
            dry_run=bool(self.dry_run),
        )

        ok = all(not analysis.error for analysis in analyses)
        payload = {
            "ok": ok,
            "method": "database_data_concept",
            "db_id": self.db_id,
            "dry_run": bool(self.dry_run),
            "log_dir": str(self.log_dir),
            "snapshot_count": len(snapshots),
            "analysis_count": len(analyses),
            "data_snapshots": [snapshot.to_dict() for snapshot in snapshots],
            "shape_grain_analyses": [analysis.to_dict() for analysis in analyses],
            "database_concept_catalog": catalog,
            "elapsed_seconds": time.perf_counter() - started_at,
        }
        write_json(output_path, payload)
        write_json(self.log_dir / "output.json", payload)
        return payload


def main() -> None:
    args = parse_args()
    timestamp = args.timestamp or build_timestamp()
    log_dir = (Path(args.log_root).resolve() / timestamp / args.db_id).resolve()
    output_path = (
        args.output_path.resolve()
        if args.output_path is not None
        else (log_dir / args.output_filename).resolve()
    )
    try:
        runner = DatabaseDataConceptRunner(
            db_id=args.db_id,
            database_root=args.database_root,
            spider2_root=args.spider2_root,
            log_dir=log_dir,
            prompt_dir=args.prompt_dir,
            model_config=args.model_config,
            reasoning_mode=args.reasoning_mode,
            sample_values_per_column=args.sample_values_per_column,
            dry_run=bool(args.dry_run),
        )
        payload = runner.run(
            output_path=output_path,
            table_filters=selected_table_names(args.table_fullname),
            max_concurrency=args.max_concurrency,
        )
        emit_step_done_log(
            prefix="DATABASE_DATA_CONCEPT",
            step="done",
            elapsed_seconds=float(payload.get("elapsed_seconds") or 0),
            ok=bool(payload.get("ok")),
            output_path=str(output_path),
            log_dir=str(log_dir),
        )
    except Exception as exc:
        log_dir.mkdir(parents=True, exist_ok=True)
        failure_payload = {
            "ok": False,
            "method": "database_data_concept",
            "db_id": args.db_id,
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "output_path": str(output_path),
            "log_dir": str(log_dir),
        }
        write_json(log_dir / "error.json", failure_payload)
        write_json(output_path, failure_payload)
        raise


if __name__ == "__main__":
    main()
