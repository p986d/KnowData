from __future__ import annotations

import argparse
import json
import random
import shutil
from pathlib import Path


DEFAULT_SOURCE_JSONL = Path(r"D:\Workspace\Spider2\spider2-snow\spider2-snow.jsonl")
DEFAULT_GOLD_SQL_DIR = Path(r"D:\Workspace\Spider2\spider2-snow\evaluation_suite\gold\sql")
DEFAULT_OUTPUT_JSON = Path(r"D:\Workspace\Spider2Test\data\input.json")
DEFAULT_OUTPUT_GROUND_TRUTH_DIR = Path(r"D:\Workspace\Spider2Test\data\ground_truth")
DEFAULT_MAX_EXTERNAL_KNOWLEDGE_CHARS = 4000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare Spider2 test samples by selecting eligible questions from spider2-snow.jsonl "
            "and copying the matching ground-truth SQL files."
        )
    )
    parser.add_argument(
        "--source-jsonl",
        type=Path,
        default=DEFAULT_SOURCE_JSONL,
        help=f"Path to spider2-snow jsonl. Default: {DEFAULT_SOURCE_JSONL}",
    )
    parser.add_argument(
        "--gold-sql-dir",
        type=Path,
        default=DEFAULT_GOLD_SQL_DIR,
        help=f"Directory containing ground-truth SQL files. Default: {DEFAULT_GOLD_SQL_DIR}",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=DEFAULT_OUTPUT_JSON,
        help=f"Output JSON list path. Default: {DEFAULT_OUTPUT_JSON}",
    )
    parser.add_argument(
        "--output-ground-truth-dir",
        type=Path,
        default=DEFAULT_OUTPUT_GROUND_TRUTH_DIR,
        help=f"Directory to copy selected SQL files into. Default: {DEFAULT_OUTPUT_GROUND_TRUTH_DIR}",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed used with --count. Default: 42",
    )
    parser.add_argument(
        "--clean-ground-truth-dir",
        action="store_true",
        help="Delete existing .sql files in the output ground-truth directory before copying.",
    )

    selection_group = parser.add_mutually_exclusive_group(required=True)
    selection_group.add_argument(
        "--count",
        type=int,
        help="Randomly select N eligible questions.",
    )
    selection_group.add_argument(
        "--instance-ids",
        nargs="+",
        help="Select questions by instance_id values.",
    )
    selection_group.add_argument(
        "--instance-id-file",
        type=Path,
        help="Path to a text file containing one instance_id per line.",
    )
    return parser.parse_args()


def load_jsonl_records(source_jsonl: Path) -> list[dict]:
    records: list[dict] = []
    with source_jsonl.open("r", encoding="utf-8") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number} of {source_jsonl}") from exc
            if not isinstance(payload, dict):
                raise ValueError(
                    f"Expected JSON object on line {line_number} of {source_jsonl}, "
                    f"got {type(payload).__name__}"
                )
            records.append(payload)
    return records


def shorten_text(text: str, limit: int = DEFAULT_MAX_EXTERNAL_KNOWLEDGE_CHARS) -> str:
    compact = text.strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def build_external_knowledge_index(
    records: list[dict],
    *,
    documents_dir: Path,
    max_chars_per_doc: int = DEFAULT_MAX_EXTERNAL_KNOWLEDGE_CHARS,
) -> dict[str, str]:
    document_names_by_db: dict[str, list[str]] = {}

    for record in records:
        db_id = str(record.get("db_id") or "").strip()
        document_name = str(record.get("external_knowledge") or "").strip()
        if not db_id:
            continue
        document_names_by_db.setdefault(db_id, [])
        if document_name and document_name not in document_names_by_db[db_id]:
            document_names_by_db[db_id].append(document_name)

    external_knowledge_by_db: dict[str, str] = {}
    for db_id, document_names in document_names_by_db.items():
        chunks: list[str] = []
        for document_name in document_names:
            document_path = documents_dir / document_name
            if not document_path.is_file():
                continue
            content = document_path.read_text(encoding="utf-8", errors="ignore")
            chunks.append(f"[{document_name}]\n{shorten_text(content, max_chars_per_doc)}")
        external_knowledge_by_db[db_id] = "\n\n".join(chunks)

    return external_knowledge_by_db


def build_eligible_index(records: list[dict], gold_sql_dir: Path) -> dict[str, dict]:
    eligible: dict[str, dict] = {}
    for record in records:
        instance_id = str(record.get("instance_id") or "").strip()
        instruction = str(record.get("instruction") or "").strip()
        db_id = str(record.get("db_id") or "").strip()
        if not instance_id or not instruction or not db_id:
            continue
        if not (gold_sql_dir / f"{instance_id}.sql").is_file():
            continue
        eligible[instance_id] = record
    return eligible


def load_instance_ids(instance_ids: list[str] | None, instance_id_file: Path | None) -> list[str]:
    raw_ids: list[str] = []
    if instance_ids:
        raw_ids.extend(instance_ids)
    if instance_id_file:
        with instance_id_file.open("r", encoding="utf-8") as file:
            for raw_line in file:
                line = raw_line.strip()
                if line:
                    raw_ids.append(line)

    deduped_ids: list[str] = []
    seen: set[str] = set()
    for instance_id in raw_ids:
        normalized = instance_id.strip()
        if normalized and normalized not in seen:
            deduped_ids.append(normalized)
            seen.add(normalized)
    return deduped_ids


def select_records(
    *,
    all_records_by_id: dict[str, dict],
    complete_records_by_id: dict[str, dict],
    eligible_by_id: dict[str, dict],
    gold_sql_dir: Path,
    count: int | None,
    instance_ids: list[str],
    seed: int,
) -> tuple[list[dict], list[str], list[str], list[str]]:
    if count is not None:
        if count <= 0:
            raise ValueError("--count must be a positive integer.")
        eligible_records = list(eligible_by_id.values())
        if count > len(eligible_records):
            raise ValueError(
                f"Requested {count} samples, but only {len(eligible_records)} eligible questions "
                f"have matching ground-truth SQL files."
            )
        rng = random.Random(seed)
        selected = rng.sample(eligible_records, count)
        selected.sort(key=lambda item: str(item["instance_id"]))
        return selected, [], [], []

    selected: list[dict] = []
    missing_ids: list[str] = []
    skipped_without_gt: list[str] = []
    skipped_incomplete_records: list[str] = []

    for instance_id in instance_ids:
        record = eligible_by_id.get(instance_id)
        if record is not None:
            selected.append(record)
            continue

        if instance_id not in all_records_by_id:
            missing_ids.append(instance_id)
        elif instance_id not in complete_records_by_id:
            skipped_incomplete_records.append(instance_id)
        else:
            sql_path = gold_sql_dir / f"{instance_id}.sql"
            if not sql_path.is_file():
                skipped_without_gt.append(instance_id)
            else:
                missing_ids.append(instance_id)

    if not selected:
        raise ValueError("No eligible questions were selected.")

    return selected, missing_ids, skipped_without_gt, skipped_incomplete_records


def build_output_payload(
    selected_records: list[dict],
    *,
    external_knowledge_by_db: dict[str, str],
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for record in selected_records:
        db_id = str(record["db_id"])
        output.append(
            {
                "question_id": str(record["instance_id"]),
                "db_id": db_id,
                "user_intent": str(record["instruction"]),
                "db_hint": "",
                "external_knowledge": external_knowledge_by_db.get(db_id, ""),
            }
        )
    return output


def clean_ground_truth_dir(output_ground_truth_dir: Path) -> None:
    if not output_ground_truth_dir.exists():
        return
    for sql_path in output_ground_truth_dir.glob("*.sql"):
        sql_path.unlink()


def copy_ground_truth_sql(
    *,
    selected_records: list[dict],
    gold_sql_dir: Path,
    output_ground_truth_dir: Path,
) -> None:
    output_ground_truth_dir.mkdir(parents=True, exist_ok=True)
    for record in selected_records:
        instance_id = str(record["instance_id"])
        source_sql = gold_sql_dir / f"{instance_id}.sql"
        target_sql = output_ground_truth_dir / source_sql.name
        shutil.copy2(source_sql, target_sql)


def write_output_json(output_json: Path, payload: list[dict[str, str]]) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    with output_json.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")


def main() -> None:
    args = parse_args()

    records = load_jsonl_records(args.source_jsonl)
    documents_dir = args.source_jsonl.parent / "resource" / "documents"
    external_knowledge_by_db = build_external_knowledge_index(
        records,
        documents_dir=documents_dir,
    )
    all_records_by_id = {
        str(record.get("instance_id") or "").strip(): record
        for record in records
        if str(record.get("instance_id") or "").strip()
    }
    complete_records_by_id = {
        str(record.get("instance_id") or "").strip(): record
        for record in records
        if (
            str(record.get("instance_id") or "").strip()
            and str(record.get("instruction") or "").strip()
            and str(record.get("db_id") or "").strip()
        )
    }
    eligible_by_id = build_eligible_index(records, args.gold_sql_dir)

    requested_ids = load_instance_ids(args.instance_ids, args.instance_id_file)
    selected_records, missing_ids, skipped_without_gt, skipped_incomplete_records = select_records(
        all_records_by_id=all_records_by_id,
        complete_records_by_id=complete_records_by_id,
        eligible_by_id=eligible_by_id,
        gold_sql_dir=args.gold_sql_dir,
        count=args.count,
        instance_ids=requested_ids,
        seed=args.seed,
    )

    if args.clean_ground_truth_dir:
        clean_ground_truth_dir(args.output_ground_truth_dir)

    output_payload = build_output_payload(
        selected_records,
        external_knowledge_by_db=external_knowledge_by_db,
    )
    write_output_json(args.output_json, output_payload)
    copy_ground_truth_sql(
        selected_records=selected_records,
        gold_sql_dir=args.gold_sql_dir,
        output_ground_truth_dir=args.output_ground_truth_dir,
    )

    print(f"Selected {len(selected_records)} eligible questions.")
    print(f"Wrote input JSON list to: {args.output_json}")
    print(f"Copied ground-truth SQL files to: {args.output_ground_truth_dir}")

    if missing_ids:
        print(
            "Skipped instance_ids not found in the dataset: "
            + ", ".join(missing_ids)
        )
    if skipped_without_gt:
        print(
            "Skipped instance_ids without matching ground-truth SQL: "
            + ", ".join(skipped_without_gt)
        )
    if skipped_incomplete_records:
        print(
            "Skipped instance_ids with incomplete source records: "
            + ", ".join(skipped_incomplete_records)
        )


if __name__ == "__main__":
    main()
