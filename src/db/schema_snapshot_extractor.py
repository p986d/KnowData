from __future__ import annotations

import argparse
import csv
import json
import logging
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from src.config import load_settings

from .db_client import SnapshotDBClient, SnapshotTableInfo, build_db_client


class DatabaseSnapshotExtractor:
    def __init__(
        self,
        client: SnapshotDBClient,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.client = client
        self.logger = logger or self._build_default_logger()

    @staticmethod
    def _build_default_logger() -> logging.Logger:
        logger = logging.getLogger("DatabaseSnapshotExtractor")
        if not logger.handlers:
            logger.setLevel(logging.INFO)
            handler = logging.StreamHandler()
            handler.setLevel(logging.INFO)
            formatter = logging.Formatter(
                "[%(asctime)s] [%(levelname)s] %(message)s",
                datefmt="%H:%M:%S",
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        return logger

    @classmethod
    def _json_safe_value(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Decimal):
            if value == value.to_integral_value():
                return int(value)
            return float(value)
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        if isinstance(value, bytes):
            return value.hex()
        if isinstance(value, dict):
            return {
                str(key): cls._json_safe_value(val)
                for key, val in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [cls._json_safe_value(item) for item in value]
        return str(value)

    def build_table_snapshot(
        self,
        table: SnapshotTableInfo,
        *,
        sample_rows: int = 5,
    ) -> dict[str, Any]:
        columns = self.client.get_table_columns(table.name)
        rows = self.client.get_table_sample_rows(table.name, limit=sample_rows)

        return {
            "table_name": self.client.format_table_name(table.name),
            "table_fullname": self.client.format_table_fullname(table.name),
            "column_names": [column.name for column in columns],
            "column_types": [column.data_type for column in columns],
            "description": [column.description for column in columns],
            "sample_rows": [
                self._json_safe_value(row)
                for row in rows
            ],
        }

    def export_snapshot(
        self,
        output_root: str | Path,
        *,
        db_id: str | None = None,
        sample_rows: int = 5,
        include_views: bool = False,
    ) -> Path:
        output_root = Path(output_root)
        db_id = db_id or self.client.get_database_name()
        schema_name = self.client.get_namespace_name()
        schema_dir = output_root / db_id
        if self.client.has_namespace() and schema_name:
            schema_dir = schema_dir / schema_name
        schema_dir.mkdir(parents=True, exist_ok=True)

        tables = self.client.list_tables(include_views=include_views)
        log_target_name = db_id
        if self.client.has_namespace() and schema_name:
            log_target_name = f"{db_id}.{schema_name}"
        self.logger.info(
            "Exporting snapshot for %s to %s",
            log_target_name,
            schema_dir,
        )

        self._write_ddl_csv(schema_dir / "DDL.csv", tables)

        total_tables = len(tables)
        for idx, table in enumerate(tables, start=1):
            self.logger.info(
                "[%s/%s] Exporting table %s",
                idx,
                total_tables,
                table.name,
            )
            payload = self.build_table_snapshot(table, sample_rows=sample_rows)
            table_path = schema_dir / f"{table.name}.json"
            with table_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=4)

        self.logger.info(
            "Snapshot export completed: %s tables written to %s",
            total_tables,
            schema_dir,
        )
        return schema_dir

    def _write_ddl_csv(
        self,
        output_path: Path,
        tables: list[SnapshotTableInfo],
    ) -> None:
        with output_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["table_name", "description", "DDL"],
            )
            writer.writeheader()

            for table in tables:
                writer.writerow(
                    {
                        "table_name": table.name,
                        "description": table.description or "",
                        "DDL": self.client.get_table_ddl(table.name),
                    }
                )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export a database snapshot in Spider-Snow compatible JSON format.",
    )
    parser.add_argument("--connection", required=True, help="Configured connection name.")
    parser.add_argument("--output-root", required=True, help="Snapshot output root directory.")
    parser.add_argument("--db-id", default=None, help="Output db id. Defaults to the client database name.")
    parser.add_argument("--config-path", default=None, help="Optional config path.")
    parser.add_argument("--env-path", default=None, help="Optional .env path.")
    parser.add_argument("--sample-rows", type=int, default=5, help="Rows sampled per table.")
    parser.add_argument(
        "--include-views",
        action="store_true",
        help="Include views when the backend supports them.",
    )
    parser.add_argument(
        "--snowflake-database-name",
        default=None,
        help="Override Snowflake database name for snapshot export.",
    )
    parser.add_argument(
        "--snowflake-schema-name",
        default=None,
        help="Override Snowflake schema name for snapshot export.",
    )
    return parser


def main() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()

    settings = load_settings(
        config_path=args.config_path,
        env_path=args.env_path,
    )
    client = build_db_client(
        args.connection,
        settings,
        snowflake_database_name=args.snowflake_database_name,
        snowflake_schema_name=args.snowflake_schema_name,
    )

    try:
        extractor = DatabaseSnapshotExtractor(client)
        extractor.export_snapshot(
            args.output_root,
            db_id=args.db_id,
            sample_rows=args.sample_rows,
            include_views=args.include_views,
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
