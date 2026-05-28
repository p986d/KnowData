from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlglot import expressions as exp

from src.utils.sqlglot_parser import SqlglotParser


@dataclass(slots=True)
class CoverageSummary:
    gold: list[str]
    predicted: list[str]
    covered: list[str]
    missing: list[str]
    extra: list[str]
    recall: float
    precision: float
    fully_covered: bool


@dataclass(slots=True)
class GoldSqlReferences:
    tables: list[str]
    resolved_columns: list[str]
    column_names: list[str]


@dataclass(slots=True)
class SchemaLinkingSelection:
    tables: list[str]
    columns: list[str]
    column_names: list[str]


@dataclass(slots=True)
class SchemaLinkingCoverageResult:
    gold: GoldSqlReferences
    predicted: SchemaLinkingSelection
    table_coverage: CoverageSummary
    resolved_column_coverage: CoverageSummary
    column_name_coverage: CoverageSummary
    fully_covered: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "gold": asdict(self.gold),
            "predicted": asdict(self.predicted),
            "table_coverage": asdict(self.table_coverage),
            "resolved_column_coverage": asdict(self.resolved_column_coverage),
            "column_name_coverage": asdict(self.column_name_coverage),
            "fully_covered": self.fully_covered,
        }


@dataclass(slots=True)
class BatchCoverageCaseResult:
    question_id: str
    case_dir: str
    schema_linking_path: str
    ground_truth_path: str | None
    ground_truth_source: str
    dialect: str | None
    ok: bool
    error: str | None = None
    coverage: SchemaLinkingCoverageResult | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "question_id": self.question_id,
            "case_dir": self.case_dir,
            "schema_linking_path": self.schema_linking_path,
            "ground_truth_path": self.ground_truth_path,
            "ground_truth_source": self.ground_truth_source,
            "dialect": self.dialect,
            "ok": self.ok,
            "error": self.error,
        }
        if self.coverage is not None:
            payload["coverage"] = self.coverage.to_payload()
        return payload


@dataclass(slots=True)
class BatchCoverageReport:
    metadata_dir: str
    ground_truth_dir: str
    total_cases: int
    ok_cases: int
    covered_cases: int
    uncovered_cases: int
    error_cases: int
    strict_table_recall_rate: float
    strict_column_recall_rate: float
    cases: list[BatchCoverageCaseResult]

    def to_payload(self) -> dict[str, Any]:
        return {
            "metadata_dir": self.metadata_dir,
            "ground_truth_dir": self.ground_truth_dir,
            "total_cases": self.total_cases,
            "ok_cases": self.ok_cases,
            "covered_cases": self.covered_cases,
            "uncovered_cases": self.uncovered_cases,
            "error_cases": self.error_cases,
            "strict_table_recall_rate": self.strict_table_recall_rate,
            "strict_column_recall_rate": self.strict_column_recall_rate,
            "cases": [item.to_payload() for item in self.cases],
        }


def remove_declare_lines(sql_script: str) -> str:
    lines = sql_script.splitlines()
    cleaned_lines = [line for line in lines if not line.strip().upper().startswith("DECLARE")]
    return "\n".join(cleaned_lines)


def normalize_identifier(value: str) -> str:
    text = str(value or "").strip().replace('"', "").replace("`", "").replace("'", "")
    return text.upper()


def normalize_qualified_name(value: str) -> str:
    parts = [normalize_identifier(part) for part in str(value or "").split(".")]
    return ".".join(part for part in parts if part)


def split_qualified_parts(value: str) -> tuple[str, ...]:
    normalized = normalize_qualified_name(value)
    if not normalized:
        return ()
    return tuple(part for part in normalized.split(".") if part)


def names_match_by_suffix(left: str, right: str) -> bool:
    left_parts = split_qualified_parts(left)
    right_parts = split_qualified_parts(right)
    if not left_parts or not right_parts:
        return False
    shorter, longer = (
        (left_parts, right_parts)
        if len(left_parts) <= len(right_parts)
        else (right_parts, left_parts)
    )
    return tuple(longer[-len(shorter) :]) == tuple(shorter)


def compute_metrics(predicted: set[str], gold: set[str]) -> tuple[float, float]:
    recall = len(predicted & gold) / len(gold) if gold else 0.0
    precision = len(predicted & gold) / len(predicted) if predicted else 0.0
    return recall, precision


def build_fuzzy_coverage_summary(predicted: set[str], gold: set[str]) -> CoverageSummary:
    covered_gold = {
        gold_name
        for gold_name in gold
        if any(names_match_by_suffix(pred_name, gold_name) for pred_name in predicted)
    }
    covered_pred = {
        pred_name
        for pred_name in predicted
        if any(names_match_by_suffix(pred_name, gold_name) for gold_name in gold)
    }
    missing = sorted(gold - covered_gold)
    extra = sorted(predicted - covered_pred)
    recall = len(covered_gold) / len(gold) if gold else 0.0
    precision = len(covered_pred) / len(predicted) if predicted else 0.0
    return CoverageSummary(
        gold=sorted(gold),
        predicted=sorted(predicted),
        covered=sorted(covered_gold),
        missing=missing,
        extra=extra,
        recall=recall,
        precision=precision,
        fully_covered=not missing,
    )


def build_coverage_summary(predicted: set[str], gold: set[str]) -> CoverageSummary:
    covered = sorted(predicted & gold)
    missing = sorted(gold - predicted)
    extra = sorted(predicted - gold)
    recall, precision = compute_metrics(predicted, gold)
    return CoverageSummary(
        gold=sorted(gold),
        predicted=sorted(predicted),
        covered=covered,
        missing=missing,
        extra=extra,
        recall=recall,
        precision=precision,
        fully_covered=not missing,
    )


def extract_gold_sql_references(
    ground_truth_sql: str,
    *,
    dialect: str | None = None,
) -> GoldSqlReferences:
    cleaned_sql = remove_declare_lines(str(ground_truth_sql or "").strip())
    expression = SqlglotParser.parse(cleaned_sql, dialect=dialect)

    cte_names = {
        normalize_identifier(cte.alias_or_name)
        for cte in expression.find_all(exp.CTE)
        if str(cte.alias_or_name or "").strip()
    }

    gold_tables: set[str] = set()
    for table in expression.find_all(exp.Table):
        table_name = normalize_qualified_name(_format_table_name(table))
        if not table_name or normalize_identifier(getattr(table, "name", "")) in cte_names:
            continue
        gold_tables.add(table_name)

    resolved_columns: set[str] = set()
    resolved_info = SqlglotParser.extract_table_columns_and_join_conditions(
        cleaned_sql,
        dialect=dialect,
    )
    for table_info in resolved_info.get("tables", []):
        if not isinstance(table_info, dict):
            continue
        table_name = normalize_qualified_name(str(table_info.get("table_name") or ""))
        if not table_name:
            continue
        for column_name in table_info.get("columns", []):
            normalized_column_name = normalize_identifier(str(column_name or ""))
            if normalized_column_name and normalized_column_name != "*":
                resolved_columns.add(f"{table_name}.{normalized_column_name}")

    gold_column_names = {
        normalize_identifier(fullname.rsplit(".", 1)[-1])
        for fullname in resolved_columns
        if str(fullname).strip()
    }
    if not gold_column_names:
        gold_column_names = {
            normalize_identifier(column.name)
            for column in expression.find_all(exp.Column)
            if str(column.name or "").strip() and normalize_identifier(column.name) != "*"
        }

    return GoldSqlReferences(
        tables=sorted(gold_tables),
        resolved_columns=sorted(resolved_columns),
        column_names=sorted(gold_column_names),
    )


def extract_schema_linking_selection(schema_linking_payload: Any) -> SchemaLinkingSelection:
    table_names: set[str] = set()
    column_names: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            linked_tables = _extract_item_fullnames(node.get("linked_tables"), kind="table")
            linked_columns = _extract_item_fullnames(node.get("linked_columns"), kind="column")
            generated_tables = _extract_item_fullnames(node.get("gen_tb"), kind="table")
            generated_columns = _extract_item_fullnames(node.get("gen_col"), kind="column")
            raw_tables = _extract_item_fullnames(node.get("tables"), kind="table")
            raw_columns = _extract_item_fullnames(node.get("columns"), kind="column")

            table_names.update(linked_tables)
            table_names.update(generated_tables)
            table_names.update(raw_tables)
            column_names.update(linked_columns)
            column_names.update(generated_columns)
            column_names.update(raw_columns)

            for value in node.values():
                if isinstance(value, (dict, list)):
                    visit(value)
            return

        if isinstance(node, list):
            for item in node:
                visit(item)

    visit(schema_linking_payload)

    predicted_column_names = {
        normalize_identifier(fullname.rsplit(".", 1)[-1])
        for fullname in column_names
        if str(fullname).strip()
    }

    return SchemaLinkingSelection(
        tables=sorted(table_names),
        columns=sorted(column_names),
        column_names=sorted(predicted_column_names),
    )


def evaluate_schema_linking_coverage(
    *,
    schema_linking_payload: Any,
    ground_truth_sql: str,
    dialect: str | None = None,
) -> SchemaLinkingCoverageResult:
    gold = extract_gold_sql_references(ground_truth_sql, dialect=dialect)
    predicted = extract_schema_linking_selection(schema_linking_payload)

    predicted_tables = set(predicted.tables)
    predicted_columns = set(predicted.columns)
    predicted_column_names = set(predicted.column_names)

    gold_tables = set(gold.tables)
    gold_resolved_columns = set(gold.resolved_columns)
    gold_column_names = set(gold.column_names)

    table_coverage = build_fuzzy_coverage_summary(predicted_tables, gold_tables)
    resolved_column_coverage = build_fuzzy_coverage_summary(
        predicted_columns,
        gold_resolved_columns,
    )
    column_name_coverage = build_coverage_summary(
        predicted_column_names,
        gold_column_names,
    )

    fully_covered = (
        table_coverage.fully_covered
        and (not gold_resolved_columns or resolved_column_coverage.fully_covered)
    )

    return SchemaLinkingCoverageResult(
        gold=gold,
        predicted=predicted,
        table_coverage=table_coverage,
        resolved_column_coverage=resolved_column_coverage,
        column_name_coverage=column_name_coverage,
        fully_covered=fully_covered,
    )


def generate_batch_coverage_report(
    *,
    metadata_dir: str | Path,
    ground_truth_dir: str | Path,
    dialect: str | None = None,
    allow_local_ground_truth_fallback: bool = True,
    schema_linking_filename: str = "schema_linking.json",
) -> BatchCoverageReport:
    resolved_metadata_dir = Path(metadata_dir).expanduser().resolve()
    resolved_ground_truth_dir = Path(ground_truth_dir).expanduser().resolve()

    if not resolved_metadata_dir.exists():
        raise FileNotFoundError(f"Metadata directory does not exist: {resolved_metadata_dir}")
    if not resolved_ground_truth_dir.exists():
        raise FileNotFoundError(
            f"Ground-truth directory does not exist: {resolved_ground_truth_dir}"
        )

    case_dirs = _discover_metadata_case_dirs(
        resolved_metadata_dir,
        schema_linking_filename=schema_linking_filename,
    )
    case_results: list[BatchCoverageCaseResult] = []

    for case_dir in case_dirs:
        schema_linking_path = case_dir / schema_linking_filename
        question_id = _read_case_question_id(case_dir)
        case_dialect = dialect or _infer_dialect(question_id)
        ground_truth_path, ground_truth_source = _resolve_ground_truth_sql_path(
            case_dir=case_dir,
            question_id=question_id,
            ground_truth_dir=resolved_ground_truth_dir,
            allow_local_fallback=allow_local_ground_truth_fallback,
        )

        if ground_truth_path is None:
            case_results.append(
                BatchCoverageCaseResult(
                    question_id=question_id,
                    case_dir=str(case_dir),
                    schema_linking_path=str(schema_linking_path),
                    ground_truth_path=None,
                    ground_truth_source=ground_truth_source,
                    dialect=case_dialect,
                    ok=False,
                    error="Ground-truth SQL file was not found.",
                )
            )
            continue

        try:
            schema_linking_payload = load_json(schema_linking_path)
            ground_truth_sql = ground_truth_path.read_text(encoding="utf-8")
            coverage = evaluate_schema_linking_coverage(
                schema_linking_payload=schema_linking_payload,
                ground_truth_sql=ground_truth_sql,
                dialect=case_dialect,
            )
        except Exception as exc:
            case_results.append(
                BatchCoverageCaseResult(
                    question_id=question_id,
                    case_dir=str(case_dir),
                    schema_linking_path=str(schema_linking_path),
                    ground_truth_path=str(ground_truth_path),
                    ground_truth_source=ground_truth_source,
                    dialect=case_dialect,
                    ok=False,
                    error=str(exc),
                )
            )
            continue

        case_results.append(
            BatchCoverageCaseResult(
                question_id=question_id,
                case_dir=str(case_dir),
                schema_linking_path=str(schema_linking_path),
                ground_truth_path=str(ground_truth_path),
                ground_truth_source=ground_truth_source,
                dialect=case_dialect,
                ok=True,
                coverage=coverage,
            )
        )

    ok_cases = sum(1 for item in case_results if item.ok)
    covered_cases = sum(
        1
        for item in case_results
        if item.ok and item.coverage is not None and item.coverage.fully_covered
    )
    uncovered_cases = sum(
        1
        for item in case_results
        if item.ok and item.coverage is not None and not item.coverage.fully_covered
    )
    error_cases = len(case_results) - ok_cases
    strict_table_recall_rate, strict_column_recall_rate = compute_strict_recall_rates(
        case_results
    )

    return BatchCoverageReport(
        metadata_dir=str(resolved_metadata_dir),
        ground_truth_dir=str(resolved_ground_truth_dir),
        total_cases=len(case_results),
        ok_cases=ok_cases,
        covered_cases=covered_cases,
        uncovered_cases=uncovered_cases,
        error_cases=error_cases,
        strict_table_recall_rate=strict_table_recall_rate,
        strict_column_recall_rate=strict_column_recall_rate,
        cases=case_results,
    )


def compute_strict_recall_rates(
    case_results: list[BatchCoverageCaseResult],
) -> tuple[float, float]:
    ok_coverages = [
        item.coverage
        for item in case_results
        if item.ok and item.coverage is not None
    ]
    if not ok_coverages:
        return 0.0, 0.0

    strict_table_recall_rate = sum(
        1.0 if coverage.table_coverage.recall == 1.0 else 0.0
        for coverage in ok_coverages
    ) / len(ok_coverages)
    strict_column_recall_rate = sum(
        1.0 if coverage.resolved_column_coverage.recall == 1.0 else 0.0
        for coverage in ok_coverages
    ) / len(ok_coverages)
    return strict_table_recall_rate, strict_column_recall_rate


def format_batch_coverage_report(
    report: BatchCoverageReport,
    *,
    show_covered_details: bool = False,
) -> str:
    lines = [
        "Schema Linking Coverage Report",
        f"metadata_dir={report.metadata_dir}",
        f"ground_truth_dir={report.ground_truth_dir}",
        (
            "summary: "
            f"total={report.total_cases} "
            f"ok={report.ok_cases} "
            f"covered={report.covered_cases} "
            f"uncovered={report.uncovered_cases} "
            f"errors={report.error_cases} "
            f"strict_table_recall_rate={report.strict_table_recall_rate:.3f} "
            f"strict_column_recall_rate={report.strict_column_recall_rate:.3f}"
        ),
        "",
    ]

    for item in report.cases:
        if not item.ok:
            lines.append(
                f"ERROR {item.question_id} dialect={item.dialect or '-'} "
                f"ground_truth_source={item.ground_truth_source} error={item.error or ''}"
            )
            continue

        assert item.coverage is not None
        coverage = item.coverage
        status = "PASS" if coverage.fully_covered else "FAIL"
        if coverage.fully_covered and not show_covered_details:
            continue

        lines.append(
            f"{status} {item.question_id} "
            f"table_precision={coverage.table_coverage.precision:.3f} "
            f"table_recall={coverage.table_coverage.recall:.3f} "
            f"column_precision={coverage.resolved_column_coverage.precision:.3f} "
            f"column_recall={coverage.resolved_column_coverage.recall:.3f}"
        )

        if coverage.fully_covered:
            continue

        if coverage.table_coverage.missing:
            lines.append(
                "  missing_tables: " + ", ".join(coverage.table_coverage.missing)
            )
        if coverage.resolved_column_coverage.missing:
            lines.append(
                "  missing_columns: "
                + ", ".join(coverage.resolved_column_coverage.missing)
            )

    if len(lines) == 5:
        lines.append("No failing cases.")

    return "\n".join(lines)


def load_json(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(
            f"Expected a JSON object at {file_path}, got {type(payload).__name__}."
        )
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate whether schema linking results cover the tables and columns used by "
            "ground-truth SQL, either for one schema-linking file or for a metadata batch."
        )
    )
    parser.add_argument("--schema-linking-path", type=Path, default=None)
    parser.add_argument("--ground-truth-sql-path", type=Path, default=None)
    parser.add_argument("--metadata-dir", type=Path, default=None)
    parser.add_argument("--ground-truth-dir", type=Path, default=None)
    parser.add_argument("--dialect", default=None)
    parser.add_argument("--output-path", type=Path, default=None)
    parser.add_argument("--schema-linking-filename", default="schema_linking.json")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--show-covered-details", action="store_true")
    parser.add_argument("--no-local-ground-truth-fallback", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    batch_mode = args.metadata_dir is not None or args.ground_truth_dir is not None
    single_mode = (
        args.schema_linking_path is not None or args.ground_truth_sql_path is not None
    )

    if batch_mode and single_mode:
        raise ValueError(
            "Use either batch mode (`--metadata-dir` and `--ground-truth-dir`) "
            "or single-file mode (`--schema-linking-path` and `--ground-truth-sql-path`)."
        )

    if batch_mode:
        if args.metadata_dir is None or args.ground_truth_dir is None:
            raise ValueError(
                "Batch mode requires both `--metadata-dir` and `--ground-truth-dir`."
            )
        report = generate_batch_coverage_report(
            metadata_dir=args.metadata_dir,
            ground_truth_dir=args.ground_truth_dir,
            dialect=args.dialect,
            allow_local_ground_truth_fallback=not args.no_local_ground_truth_fallback,
            schema_linking_filename=args.schema_linking_filename,
        )
        payload = report.to_payload()
        rendered_text = format_batch_coverage_report(
            report,
            show_covered_details=args.show_covered_details,
        )
    else:
        if args.schema_linking_path is None or args.ground_truth_sql_path is None:
            raise ValueError(
                "Single-file mode requires both `--schema-linking-path` and "
                "`--ground-truth-sql-path`."
            )
        schema_linking_payload = load_json(args.schema_linking_path)
        ground_truth_sql = args.ground_truth_sql_path.read_text(encoding="utf-8")
        result = evaluate_schema_linking_coverage(
            schema_linking_payload=schema_linking_payload,
            ground_truth_sql=ground_truth_sql,
            dialect=args.dialect,
        )
        payload = result.to_payload()
        rendered_text = json.dumps(payload, ensure_ascii=False, indent=2)

    if args.output_path is not None:
        args.output_path.parent.mkdir(parents=True, exist_ok=True)
        output_text = json.dumps(payload, ensure_ascii=False, indent=2) if args.json else rendered_text
        args.output_path.write_text(output_text, encoding="utf-8")
        if not args.json:
            print(rendered_text)
        return

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print(rendered_text)


def _format_table_name(table: exp.Table) -> str:
    parts = [
        str(part).strip()
        for part in (
            getattr(table, "catalog", None),
            getattr(table, "db", None),
            getattr(table, "name", None),
        )
        if str(part or "").strip()
    ]
    return ".".join(parts)


def _extract_item_fullnames(items: Any, *, kind: str) -> set[str]:
    if not isinstance(items, list):
        return set()

    fullnames: set[str] = set()
    for item in items:
        fullname = _extract_item_fullname(item, kind=kind)
        if fullname:
            fullnames.add(fullname)
    return fullnames


def _extract_item_fullname(item: Any, *, kind: str) -> str:
    if isinstance(item, str):
        return normalize_qualified_name(item)
    if not isinstance(item, dict):
        return ""

    for key in ("fullname", "full_name", "name"):
        candidate = normalize_qualified_name(str(item.get(key) or ""))
        if candidate:
            return candidate

    if kind == "table":
        parts = [
            normalize_identifier(str(item.get(key) or ""))
            for key in ("catalog", "database", "db", "schema", "table")
        ]
    else:
        parts = [
            normalize_identifier(str(item.get(key) or ""))
            for key in ("catalog", "database", "db", "schema", "table", "column")
        ]

    normalized_parts = [part for part in parts if part]
    return ".".join(normalized_parts)


def _discover_metadata_case_dirs(
    metadata_dir: Path,
    *,
    schema_linking_filename: str = "schema_linking.json",
) -> list[Path]:
    if metadata_dir.is_file():
        raise ValueError(f"Expected a metadata directory, got file: {metadata_dir}")

    direct_schema_linking = metadata_dir / schema_linking_filename
    if direct_schema_linking.exists():
        return [metadata_dir]

    case_dirs = sorted(
        {
            path.parent.resolve()
            for path in metadata_dir.rglob(schema_linking_filename)
        }
    )
    if not case_dirs:
        raise FileNotFoundError(
            f"No `{schema_linking_filename}` files were found under {metadata_dir}"
        )
    return case_dirs


def _read_case_question_id(case_dir: Path) -> str:
    for filename in ("input.json", "run_context.json"):
        candidate_path = case_dir / filename
        if not candidate_path.exists():
            continue
        try:
            payload = load_json(candidate_path)
        except Exception:
            continue
        question_id = str(payload.get("question_id") or payload.get("instance_id") or "").strip()
        if question_id:
            return question_id

    folder_name = case_dir.name
    marker = "_20"
    if marker in folder_name:
        return folder_name.split(marker, 1)[0]
    return folder_name


def _infer_dialect(question_id: str) -> str | None:
    normalized = str(question_id or "").strip().casefold()
    if normalized.startswith("sf"):
        return "snowflake"
    if normalized.startswith("local"):
        return "sqlite"
    if normalized.startswith("bq") or normalized.startswith("ga"):
        return "bigquery"
    return None


def _resolve_ground_truth_sql_path(
    *,
    case_dir: Path,
    question_id: str,
    ground_truth_dir: Path,
    allow_local_fallback: bool,
) -> tuple[Path | None, str]:
    candidate = ground_truth_dir / f"{question_id}.sql"
    if candidate.exists():
        return candidate.resolve(), "ground_truth_dir"

    local_candidate = case_dir / "ground_truth.sql"
    if allow_local_fallback and local_candidate.exists():
        return local_candidate.resolve(), "case_dir_fallback"

    return None, "missing"


if __name__ == "__main__":
    main()
