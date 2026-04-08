from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

import snowflake.connector

from src.config import SnowflakeConfig


class SnowflakeClient:
    def __init__(
        self,
        config: SnowflakeConfig,
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
        logger = logging.getLogger("SnowflakeClient")
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
        """构造 Snowflake 连接参数"""
        conn_kwargs: dict[str, Any] = {
            "account": self.config.account,
            "user": self.config.user,
            "password": self.config.password,
        }

        if self.config.warehouse:
            conn_kwargs["warehouse"] = self.config.warehouse
        if self.config.role:
            conn_kwargs["role"] = self.config.role
        if self.config.database:
            conn_kwargs["database"] = self.config.database
        if self.config.schema:
            conn_kwargs["schema"] = self.config.schema

        return conn_kwargs

    def _connect(self) -> None:
        """建立连接"""
        if self.conn is None:
            self.logger.info("Connecting to Snowflake...")
            self.conn = snowflake.connector.connect(**self._build_conn_kwargs())
            self.logger.info("Snowflake connection established.")

    def close(self) -> None:
        """关闭连接"""
        if self.conn is not None:
            self.conn.close()
            self.conn = None
            self.logger.info("Snowflake connection closed.")

    def __enter__(self) -> "SnowflakeClient":
        if self.conn is None:
            self._connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def execute_sql(
        self,
        sql: str,
        params: Optional[dict[str, Any] | tuple[Any, ...]] = None,
    ) -> list[dict[str, Any]]:
        """
        执行 SQL，并将结果转为 list[dict]
        """
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

            col_names = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            return [dict(zip(col_names, row)) for row in rows]
        finally:
            if cur is not None:
                cur.close()

    def debug_session(self) -> dict[str, Any]:
        """查看当前会话的 role / secondary roles / warehouse"""
        return {
            "current_role": self.execute_sql(
                "SELECT CURRENT_ROLE() AS current_role"
            ),
            "current_secondary_roles": self.execute_sql(
                "SELECT CURRENT_SECONDARY_ROLES() AS current_secondary_roles"
            ),
            "current_warehouse": self.execute_sql(
                "SELECT CURRENT_WAREHOUSE() AS current_warehouse"
            ),
        }

    @staticmethod
    def _get(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
        """大小写不敏感地取字段"""
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
        """对象名加双引号，处理大小写和特殊字符"""
        return f'"{name.replace(chr(34), chr(34) * 2)}"'

    @staticmethod
    def _sql_literal(value: str) -> str:
        """字符串字面量转义"""
        return "'" + value.replace("'", "''") + "'"

    def _fq_table(self, database_name: str, schema_name: str, table_name: str) -> str:
        return ".".join(
            [
                self._quote_ident(database_name),
                self._quote_ident(schema_name),
                self._quote_ident(table_name),
            ]
        )

    def _fq_info_schema(self, database_name: str, view_name: str) -> str:
        return ".".join(
            [
                self._quote_ident(database_name),
                self._quote_ident("INFORMATION_SCHEMA"),
                self._quote_ident(view_name),
            ]
        )

    @staticmethod
    def _unquote_ident(name: str) -> str:
        name = name.strip()
        if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
            return name[1:-1].replace('""', '"')
        return name

    @classmethod
    def _split_qualified_name(cls, text: str) -> list[str]:
        """按 . 切分对象名，但忽略双引号内的点"""
        parts = []
        buf = []
        in_quotes = False

        for ch in text.strip():
            if ch == '"':
                in_quotes = not in_quotes
                buf.append(ch)
            elif ch == "." and not in_quotes:
                part = "".join(buf).strip()
                if part:
                    parts.append(cls._unquote_ident(part))
                buf = []
            else:
                buf.append(ch)

        part = "".join(buf).strip()
        if part:
            parts.append(cls._unquote_ident(part))

        return parts

    @staticmethod
    def _split_column_list(text: str) -> list[str]:
        """拆分括号里的列列表"""
        parts = []
        buf = []
        in_quotes = False

        for ch in text.strip():
            if ch == '"':
                in_quotes = not in_quotes
                buf.append(ch)
            elif ch == "," and not in_quotes:
                part = "".join(buf).strip()
                if part:
                    parts.append(part)
                buf = []
            else:
                buf.append(ch)

        part = "".join(buf).strip()
        if part:
            parts.append(part)

        return [p.strip().strip('"').replace('""', '"') for p in parts]

    def list_databases(self) -> list[dict[str, Any]]:
        """列出当前可访问的数据库"""
        return self.execute_sql("SHOW DATABASES")

    def export_databases(self, output_path: str | Path) -> list[dict[str, Any]]:
        """
        只导出数据库名到 JSON 文件
        """
        raw_databases = self.list_databases()
        databases = [{"database_name": row["name"]} for row in raw_databases]

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with output_path.open("w", encoding="utf-8") as f:
            json.dump(databases, f, ensure_ascii=False, indent=2, default=str)

        self.logger.info("Exported %s databases to %s", len(databases), output_path)
        return databases

    def list_tables_in_database(
        self,
        database_name: str,
        include_views: bool = False,
    ) -> list[dict[str, Any]]:
        """
        列出数据库中的表。
        默认不包含 VIEW / MATERIALIZED VIEW。
        """
        tables_view = self._fq_info_schema(database_name, "TABLES")

        type_filter = ""
        if not include_views:
            type_filter = """
              AND TABLE_TYPE NOT IN ('VIEW', 'MATERIALIZED VIEW')
            """

        sql = f"""
        SELECT
            TABLE_SCHEMA,
            TABLE_NAME,
            TABLE_TYPE,
            ROW_COUNT,
            BYTES,
            COMMENT
        FROM {tables_view}
        WHERE TABLE_SCHEMA <> 'INFORMATION_SCHEMA'
          {type_filter}
        ORDER BY TABLE_SCHEMA, TABLE_NAME
        """
        return self.execute_sql(sql)

    def get_table_columns(
        self,
        database_name: str,
        schema_name: str,
        table_name: str,
    ) -> list[dict[str, Any]]:
        columns_view = self._fq_info_schema(database_name, "COLUMNS")

        sql = f"""
        SELECT
            COLUMN_NAME,
            DATA_TYPE,
            IS_NULLABLE,
            ORDINAL_POSITION,
            COMMENT
        FROM {columns_view}
        WHERE TABLE_SCHEMA = {self._sql_literal(schema_name)}
          AND TABLE_NAME = {self._sql_literal(table_name)}
        ORDER BY ORDINAL_POSITION
        """
        return self.execute_sql(sql)

    def get_table_ddl(
        self,
        database_name: str,
        schema_name: str,
        table_name: str,
    ) -> str:
        fq_table = self._fq_table(database_name, schema_name, table_name)
        sql = f"""
        SELECT GET_DDL('TABLE', {self._sql_literal(fq_table)}) AS DDL
        """
        rows = self.execute_sql(sql)
        if not rows:
            return ""
        return self._get(rows[0], "DDL", "ddl", default="") or ""

    def parse_keys_from_ddl(self, ddl: str) -> dict[str, Any]:
        """
        从 CREATE TABLE DDL 里解析主键/外键
        """
        result = {
            "primary_keys": [],
            "foreign_keys": [],
        }

        if not ddl:
            return result

        ddl_one_line = re.sub(r"\s+", " ", ddl)

        pk_match = re.search(
            r"PRIMARY\s+KEY\s*\((.*?)\)",
            ddl_one_line,
            flags=re.IGNORECASE,
        )
        if pk_match:
            result["primary_keys"] = self._split_column_list(pk_match.group(1))

        fk_pattern = re.compile(
            r'(?:CONSTRAINT\s+("?[^"\s(]+"?)\s+)?'
            r'FOREIGN\s+KEY\s*\((.*?)\)\s+'
            r'REFERENCES\s+((?:"[^"]+"|[A-Za-z0-9_$]+)'
            r'(?:\.(?:"[^"]+"|[A-Za-z0-9_$]+)){0,2})\s*'
            r'\((.*?)\)',
            flags=re.IGNORECASE,
        )

        for match in fk_pattern.finditer(ddl_one_line):
            constraint_name = match.group(1)
            local_cols = self._split_column_list(match.group(2))
            ref_name = match.group(3)
            ref_cols = self._split_column_list(match.group(4))

            ref_parts = self._split_qualified_name(ref_name)
            ref_db = None
            ref_schema = None
            ref_table = None

            if len(ref_parts) == 3:
                ref_db, ref_schema, ref_table = ref_parts
            elif len(ref_parts) == 2:
                ref_schema, ref_table = ref_parts
            elif len(ref_parts) == 1:
                ref_table = ref_parts[0]

            result["foreign_keys"].append(
                {
                    "constraint_name": None if not constraint_name else self._unquote_ident(constraint_name),
                    "columns": local_cols,
                    "referenced_table": {
                        "database_name": ref_db,
                        "schema_name": ref_schema,
                        "table_name": ref_table,
                    },
                    "referenced_columns": ref_cols,
                }
            )

        return result

    def get_table_sample_rows(
        self,
        database_name: str,
        schema_name: str,
        table_name: str,
        sample_rows: int = 500,
        use_random_sample: bool = False,
    ) -> list[dict[str, Any]]:
        """
        获取表的样本行。
        use_random_sample=True -> SAMPLE (N ROWS)
        use_random_sample=False -> LIMIT N
        """
        fq_table = self._fq_table(database_name, schema_name, table_name)

        if use_random_sample:
            sql = f"""
            SELECT *
            FROM {fq_table}
            SAMPLE ({int(sample_rows)} ROWS)
            """
        else:
            sql = f"""
            SELECT *
            FROM {fq_table}
            LIMIT {int(sample_rows)}
            """
        return self.execute_sql(sql)

    def _build_column_samples_from_rows(
        self,
        rows: list[dict[str, Any]],
        column_names: list[str],
        sample_value_count: int = 3,
    ) -> dict[str, list[Any]]:
        """
        从一批样本行中，为每个列提取最多 sample_value_count 个非空去重值
        """
        result: dict[str, list[Any]] = {col: [] for col in column_names}

        for row in rows:
            for col in column_names:
                val = self._get(row, col, default=None)
                if val is None:
                    continue

                current = result[col]
                if val not in current:
                    current.append(val)

        for col in result:
            result[col] = result[col][:sample_value_count]

        return result

    def build_table_profile(
        self,
        database_name: str,
        schema_name: str,
        table_name: str,
        sample_rows: int = 500,
        sample_value_count: int = 3,
        use_random_sample: bool = True,
    ) -> dict[str, Any]:
        """
        单表画像：
        - 列信息
        - 每列样本值
        - 主键 / 外键
        - 1 行样例行
        """
        columns_meta = self.get_table_columns(database_name, schema_name, table_name)
        ddl = self.get_table_ddl(database_name, schema_name, table_name)
        keys = self.parse_keys_from_ddl(ddl)

        sampled_rows = self.get_table_sample_rows(
            database_name=database_name,
            schema_name=schema_name,
            table_name=table_name,
            sample_rows=sample_rows,
            use_random_sample=use_random_sample,
        )

        column_names = [
            self._get(col, "COLUMN_NAME", "column_name")
            for col in columns_meta
        ]
        column_samples = self._build_column_samples_from_rows(
            sampled_rows,
            column_names=column_names,
            sample_value_count=sample_value_count,
        )

        sample_row = sampled_rows[0] if sampled_rows else {}

        columns = []
        for col in columns_meta:
            col_name = self._get(col, "COLUMN_NAME", "column_name")
            columns.append(
                {
                    "column_name": col_name,
                    "data_type": self._get(col, "DATA_TYPE", "data_type"),
                    "is_nullable": self._get(col, "IS_NULLABLE", "is_nullable"),
                    "ordinal_position": self._get(col, "ORDINAL_POSITION", "ordinal_position"),
                    "comment": self._get(col, "COMMENT", "comment"),
                    "sample_values": column_samples.get(col_name, []),
                }
            )

        return {
            "database_name": database_name,
            "schema_name": schema_name,
            "table_name": table_name,
            "columns": columns,
            "primary_keys": keys["primary_keys"],
            "foreign_keys": keys["foreign_keys"],
            "sample_row": sample_row,
            "sampled_row_count": len(sampled_rows),
        }

    def export_database_catalog(
        self,
        database_name: str,
        output_path: str | Path,
        include_views: bool = False,
        sample_rows: int = 500,
        sample_value_count: int = 3,
        use_random_sample: bool = True,
    ) -> dict[str, Any]:
        """
        导出一个数据库的表级画像 JSON
        """
        self.logger.info("Start exporting database catalog: %s", database_name)

        tables = self.list_tables_in_database(
            database_name=database_name,
            include_views=include_views,
        )

        total_tables = len(tables)
        self.logger.info("Found %s tables in database %s", total_tables, database_name)

        result = {
            "database_name": database_name,
            "table_count": total_tables,
            "sample_rows_per_table": sample_rows,
            "sample_value_count_per_column": sample_value_count,
            "tables": [],
        }

        for idx, tbl in enumerate(tables, start=1):
            schema_name = self._get(tbl, "TABLE_SCHEMA", "table_schema")
            table_name = self._get(tbl, "TABLE_NAME", "table_name")
            self.logger.info(
                "[%s/%s] Processing %s.%s",
                idx,
                total_tables,
                schema_name,
                table_name,
            )

            try:
                profile = self.build_table_profile(
                    database_name=database_name,
                    schema_name=schema_name,
                    table_name=table_name,
                    sample_rows=sample_rows,
                    sample_value_count=sample_value_count,
                    use_random_sample=use_random_sample,
                )
                profile["table_type"] = self._get(tbl, "TABLE_TYPE", "table_type")
                profile["row_count"] = self._get(tbl, "ROW_COUNT", "row_count")
                profile["bytes"] = self._get(tbl, "BYTES", "bytes")
                profile["table_comment"] = self._get(tbl, "COMMENT", "comment")
            except Exception as e:
                self.logger.exception(
                    "Failed to process table %s.%s: %s",
                    schema_name,
                    table_name,
                    e,
                )
                profile = {
                    "database_name": database_name,
                    "schema_name": schema_name,
                    "table_name": table_name,
                    "error": str(e),
                }

            result["tables"].append(profile)

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with output_path.open("w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)

        self.logger.info("Database catalog exported to %s", output_path)
        return result