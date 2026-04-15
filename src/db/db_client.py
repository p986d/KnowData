from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from src.config import Settings

from .mysql_client import MySQLClient
from .snowflake_client import SnowflakeClient


@dataclass(slots=True, frozen=True)
class SnapshotTableInfo:
    name: str
    description: str | None = None


@dataclass(slots=True, frozen=True)
class SnapshotColumnInfo:
    name: str
    data_type: str | None = None
    description: str | None = None


@runtime_checkable
class SnapshotDBClient(Protocol):
    def get_database_name(self) -> str: ...

    def get_namespace_name(self) -> str: ...

    def has_namespace(self) -> bool: ...

    def list_tables(self, include_views: bool = False) -> list[SnapshotTableInfo]: ...

    def get_table_columns(self, table_name: str) -> list[SnapshotColumnInfo]: ...

    def get_table_ddl(self, table_name: str) -> str: ...

    def get_table_sample_rows(
        self,
        table_name: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]: ...

    def format_table_name(self, table_name: str) -> str: ...

    def format_table_fullname(self, table_name: str) -> str: ...

    def close(self) -> None: ...


class BaseSnapshotClient:
    def __enter__(self) -> "BaseSnapshotClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


class MySQLSnapshotClient(BaseSnapshotClient):
    def __init__(self, client: MySQLClient) -> None:
        self.client = client

    def get_database_name(self) -> str:
        if not self.client.config.database:
            raise ValueError("MySQL database is not configured.")
        return self.client.config.database

    def get_namespace_name(self) -> str:
        return ""

    def has_namespace(self) -> bool:
        return False

    def list_tables(self, include_views: bool = False) -> list[SnapshotTableInfo]:
        rows = self.client.list_tables(include_views=include_views)
        return [
            SnapshotTableInfo(
                name=str(row["TABLE_NAME"]),
                description=(row.get("TABLE_COMMENT") or None),
            )
            for row in rows
        ]

    def get_table_columns(self, table_name: str) -> list[SnapshotColumnInfo]:
        rows = self.client.get_table_columns(table_name)
        return [
            SnapshotColumnInfo(
                name=str(row["COLUMN_NAME"]),
                data_type=str(row.get("COLUMN_TYPE") or row.get("DATA_TYPE") or ""),
                description=(row.get("COLUMN_COMMENT") or None),
            )
            for row in rows
        ]

    def get_table_ddl(self, table_name: str) -> str:
        return self.client.get_table_ddl(table_name)

    def get_table_sample_rows(
        self,
        table_name: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        return self.client.get_table_sample_rows(table_name, sample_rows=limit)

    def format_table_name(self, table_name: str) -> str:
        return str(table_name)

    def format_table_fullname(self, table_name: str) -> str:
        return f"{self.get_database_name()}.{table_name}"

    def close(self) -> None:
        self.client.close()


class SnowflakeSnapshotClient(BaseSnapshotClient):
    def __init__(
        self,
        client: SnowflakeClient,
        *,
        database_name: str,
        schema_name: str,
    ) -> None:
        self.client = client
        self.database_name = database_name
        self.schema_name = schema_name

    def get_database_name(self) -> str:
        return self.database_name

    def get_namespace_name(self) -> str:
        return self.schema_name

    def has_namespace(self) -> bool:
        return True

    def list_tables(self, include_views: bool = False) -> list[SnapshotTableInfo]:
        rows = self.client.list_tables_in_database(
            database_name=self.database_name,
            include_views=include_views,
        )
        filtered_rows = [
            row
            for row in rows
            if str(row["TABLE_SCHEMA"]) == self.schema_name
        ]
        return [
            SnapshotTableInfo(
                name=str(row["TABLE_NAME"]),
                description=(row.get("COMMENT") or None),
            )
            for row in filtered_rows
        ]

    def get_table_columns(self, table_name: str) -> list[SnapshotColumnInfo]:
        rows = self.client.get_table_columns(
            database_name=self.database_name,
            schema_name=self.schema_name,
            table_name=table_name,
        )
        return [
            SnapshotColumnInfo(
                name=str(row["COLUMN_NAME"]),
                data_type=str(row.get("DATA_TYPE") or ""),
                description=(row.get("COMMENT") or None),
            )
            for row in rows
        ]

    def get_table_ddl(self, table_name: str) -> str:
        return self.client.get_table_ddl(
            database_name=self.database_name,
            schema_name=self.schema_name,
            table_name=table_name,
        )

    def get_table_sample_rows(
        self,
        table_name: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        return self.client.get_table_sample_rows(
            database_name=self.database_name,
            schema_name=self.schema_name,
            table_name=table_name,
            sample_rows=limit,
            use_random_sample=False,
        )

    def format_table_name(self, table_name: str) -> str:
        return f"{self.get_namespace_name()}.{table_name}"

    def format_table_fullname(self, table_name: str) -> str:
        return f"{self.get_database_name()}.{self.get_namespace_name()}.{table_name}"

    def close(self) -> None:
        self.client.close()


def build_db_client(
    connection_name: str,
    settings: Settings,
    *,
    logger: Optional[logging.Logger] = None,
    snowflake_database_name: str | None = None,
    snowflake_schema_name: str | None = None,
) -> SnapshotDBClient:
    if connection_name == "mysql_sy_test":
        return MySQLSnapshotClient(
            MySQLClient(settings.mysql_sy_test, logger=logger)
        )

    if connection_name == "snowflake":
        database_name = snowflake_database_name or settings.snowflake.database
        schema_name = snowflake_schema_name or settings.snowflake.schema
        if not database_name:
            raise ValueError(
                "snowflake database_name is required when building a snapshot client."
            )
        if not schema_name:
            raise ValueError(
                "snowflake schema_name is required when building a snapshot client."
            )
        return SnowflakeSnapshotClient(
            SnowflakeClient(settings.snowflake, logger=logger),
            database_name=database_name,
            schema_name=schema_name,
        )

    raise KeyError(f"Unsupported database connection: {connection_name}")
