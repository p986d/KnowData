from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import expressions as exp
from sqlglot.errors import ParseError


class SqlglotParser:
    """Utility methods for parsing SQL strings with sqlglot."""

    @classmethod
    def parse(cls, sql: str, dialect: str | None = None) -> exp.Expression:
        """
        Parse a SQL string and return the sqlglot AST node.

        Args:
            sql: Raw SQL string.
            dialect: Optional sqlglot dialect name, such as "mysql" or "snowflake".
        """
        normalized_sql = cls._normalize_sql(sql)

        try:
            return sqlglot.parse_one(normalized_sql, read=dialect)
        except ParseError as exc:
            raise ValueError(f"Failed to parse SQL with sqlglot: {exc}") from exc

    @classmethod
    def parse_to_dict(cls, sql: str, dialect: str | None = None) -> dict[str, Any]:
        """
        Parse a SQL string and return a serializable AST dictionary.
        """
        return cls.parse(sql=sql, dialect=dialect).dump()

    @classmethod
    def parse_to_sql(
        cls,
        sql: str,
        dialect: str | None = None,
        pretty: bool = False,
    ) -> str:
        """
        Parse a SQL string and regenerate normalized SQL text.
        """
        return cls.parse(sql=sql, dialect=dialect).sql(pretty=pretty)

    @classmethod
    def extract_cte_column_comment_dict(
        cls,
        sql: str,
        dialect: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """
        Extract a CTE metadata dictionary from a SQL string.

        Returns:
            {
                "cte_name": {
                    "columns": ["col_a", "col_b"],
                    "comment": "cte description",
                }
            }
        """
        expression = cls.parse(sql=sql, dialect=dialect)
        with_expression = expression.args.get("with")
        if with_expression is None:
            return {}

        cte_dict: dict[str, dict[str, Any]] = {}
        for cte in with_expression.expressions:
            cte_name = cte.alias_or_name
            if not cte_name:
                continue

            cte_dict[cte_name] = {
                "columns": cls._extract_cte_columns(cte),
                "comment": cls._extract_cte_comment(cte),
            }

        return cte_dict

    @staticmethod
    def _normalize_sql(sql: str) -> str:
        if sql is None:
            raise ValueError("SQL text cannot be None.")

        normalized_sql = sql.strip()
        if not normalized_sql:
            raise ValueError("SQL text cannot be empty.")

        return normalized_sql

    @staticmethod
    def _extract_cte_columns(cte: exp.CTE) -> list[str]:
        alias = cte.args.get("alias")
        alias_columns = alias.args.get("columns") if alias is not None else None
        if alias_columns:
            return [column.name for column in alias_columns if column.name]

        query = cte.this
        selects = getattr(query, "selects", None) or []
        columns: list[str] = []

        for select_expression in selects:
            column_name = select_expression.alias_or_name
            if column_name:
                columns.append(column_name)
                continue

            columns.append(select_expression.sql())

        return columns

    @staticmethod
    def _extract_cte_comment(cte: exp.CTE) -> str | None:
        alias = cte.args.get("alias")
        raw_comments = []

        if alias is not None and alias.comments:
            raw_comments = alias.comments
        elif cte.comments:
            raw_comments = cte.comments

        comments = [comment.strip() for comment in raw_comments if comment and comment.strip()]
        if not comments:
            return None

        return "\n".join(comments)
