from __future__ import annotations

import logging
from typing import Any, Optional

import pymysql
from pymysql.cursors import DictCursor

from src.config import MySQLConfig


class MySQLClient:
    def __init__(
        self,
        config: MySQLConfig,
        logger: Optional[logging.Logger] = None,
        auto_connect: bool = True,
    ) -> None:
        self.config = config
        self.conn = None
        self.logger = logger or self._build_default_logger()

        if auto_connect:
            self._connect()

    @staticmethod
    def _build_default_logger() -> logging.Logger:
        logger = logging.getLogger("MySQLClient")
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

    def _build_conn_kwargs(self) -> dict[str, Any]:
        missing_fields = self.config.missing_fields()
        if missing_fields:
            raise ValueError(
                "MySQL config is incomplete: "
                + ", ".join(missing_fields)
            )

        return {
            "host": self.config.host,
            "port": self.config.port,
            "user": self.config.user,
            "password": self.config.password,
            "database": self.config.database,
            "charset": "utf8mb4",
            "autocommit": True,
            "cursorclass": DictCursor,
        }

    def _connect(self) -> None:
        if self.conn is None:
            self.logger.info("Connecting to MySQL...")
            self.conn = pymysql.connect(**self._build_conn_kwargs())
            self.logger.info("MySQL connection established.")

    @staticmethod
    def _get(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
        for key in keys:
            if key in row:
                return row[key]
            if key.lower() in row:
                return row[key.lower()]
            if key.upper() in row:
                return row[key.upper()]
        return default

    @staticmethod
    def _quote_ident(name: str) -> str:
        return f"`{name.replace('`', '``')}`"

    def _fq_table(self, table_name: str) -> str:
        database_name = self.config.database
        if not database_name:
            raise ValueError("MySQL database is not configured.")
        return ".".join(
            [
                self._quote_ident(database_name),
                self._quote_ident(table_name),
            ]
        )

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None
            self.logger.info("MySQL connection closed.")

    def __enter__(self) -> "MySQLClient":
        if self.conn is None:
            self._connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def ping(self) -> bool:
        if self.conn is None:
            self._connect()

        self.conn.ping(reconnect=True)
        return True

    def execute_sql(
        self,
        sql: str,
        params: Optional[dict[str, Any] | tuple[Any, ...] | list[Any]] = None,
    ) -> list[dict[str, Any]]:
        if self.conn is None:
            self._connect()

        cur = None
        try:
            cur = self.conn.cursor()
            if params is None:
                cur.execute(sql)
            else:
                cur.execute(sql, params)

            if cur.description is None:
                return []

            rows = cur.fetchall()
            return [dict(row) for row in rows]
        finally:
            if cur is not None:
                cur.close()

    def list_tables(self, include_views: bool = False) -> list[dict[str, Any]]:
        database_name = self.config.database
        if not database_name:
            raise ValueError("MySQL database is not configured.")

        type_filter = ""
        if not include_views:
            type_filter = "AND TABLE_TYPE = 'BASE TABLE'"

        sql = f"""
        SELECT
            TABLE_NAME,
            TABLE_TYPE,
            TABLE_COMMENT
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s
          {type_filter}
        ORDER BY TABLE_NAME
        """
        return self.execute_sql(sql, (database_name,))

    def get_table_columns(self, table_name: str) -> list[dict[str, Any]]:
        database_name = self.config.database
        if not database_name:
            raise ValueError("MySQL database is not configured.")

        sql = """
        SELECT
            COLUMN_NAME,
            DATA_TYPE,
            COLUMN_TYPE,
            IS_NULLABLE,
            ORDINAL_POSITION,
            COLUMN_COMMENT
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s
          AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """
        return self.execute_sql(sql, (database_name, table_name))

    def get_table_ddl(self, table_name: str) -> str:
        columns = self.get_table_columns(table_name)
        if not columns:
            return ""

        lines = [f"create or replace TABLE {table_name} ("]
        for idx, column in enumerate(columns):
            column_name = self._get(column, "COLUMN_NAME", "column_name")
            column_type = self._get(
                column,
                "COLUMN_TYPE",
                "column_type",
                default=self._get(column, "DATA_TYPE", "data_type", default="TEXT"),
            )
            suffix = "," if idx < len(columns) - 1 else ""
            lines.append(f'    "{column_name}" {column_type}{suffix}')
        lines.append(");")
        return "\n".join(lines)

    def get_table_sample_rows(
        self,
        table_name: str,
        sample_rows: int = 5,
    ) -> list[dict[str, Any]]:
        sample_rows = max(int(sample_rows), 0)
        if sample_rows == 0:
            return []

        sql = f"""
        SELECT *
        FROM {self._fq_table(table_name)}
        LIMIT {sample_rows}
        """
        return self.execute_sql(sql)
