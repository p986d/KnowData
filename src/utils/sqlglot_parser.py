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

    @classmethod
    def extract_table_columns_and_join_conditions(
        cls,
        sql: str,
        dialect: str | None = None,
    ) -> dict[str, Any]:
        """
        Extract referenced table columns and join conditions from a SQL string.

        Returns:
            {
                "tables": [
                    {
                        "table_name": "orders",
                        "table_alias": ["o"],
                        "columns": ["order_id", "customer_id"],
                    }
                ],
                "join_conditions": ["o.customer_id = c.customer_id"],
            }
        """
        expression = cls.parse(sql=sql, dialect=dialect)
        cte_names = cls._extract_cte_names(expression)

        tables: list[dict[str, Any]] = []
        table_index_by_name: dict[str, int] = {}
        table_index_by_alias: dict[str, int] = {}
        table_indexes_by_name: dict[str, list[int]] = {}
        column_seen_by_index: dict[int, set[str]] = {}
        alias_seen_by_index: dict[int, set[str]] = {}
        select_scope_cache: dict[int, dict[str, Any]] = {}

        for table in expression.find_all(exp.Table):
            table_name = cls._format_table_name(table)
            if table_name.casefold() in cte_names:
                continue
            table_alias = cls._extract_table_alias(table)
            table_index = table_index_by_name.get(table_name)
            if table_index is None:
                table_index = len(tables)
                table_index_by_name[table_name] = table_index
                tables.append(
                    {
                        "table_name": table_name,
                        "table_alias": [],
                        "columns": [],
                    }
                )
                column_seen_by_index[table_index] = set()
                alias_seen_by_index[table_index] = set()
                table_indexes_by_name[table_name] = [table_index]

            if table_alias and table_alias not in alias_seen_by_index[table_index]:
                tables[table_index]["table_alias"].append(table_alias)
                alias_seen_by_index[table_index].add(table_alias)
                table_index_by_alias[table_alias] = table_index

        for column in expression.find_all(exp.Column):
            column_name = str(column.name or "").strip()
            if not column_name:
                continue

            nearest_select = cls._find_nearest_select(column)
            scope_info = cls._resolve_select_scope_info(
                select_expression=nearest_select,
                cte_names=cte_names,
                table_index_by_alias=table_index_by_alias,
                table_indexes_by_name=table_indexes_by_name,
                cache=select_scope_cache,
            )
            if cls._is_explicit_select_alias_reference(column=column, scope_info=scope_info):
                continue
            target_index = cls._resolve_column_table_index(
                column=column,
                scope_info=scope_info,
            )
            if target_index is None:
                continue

            if column_name in column_seen_by_index[target_index]:
                continue

            tables[target_index]["columns"].append(column_name)
            column_seen_by_index[target_index].add(column_name)

        join_conditions: list[str] = []
        seen_conditions: set[str] = set()
        for join in expression.find_all(exp.Join):
            condition = cls._extract_join_condition(join)
            if not condition or condition in seen_conditions:
                continue
            join_conditions.append(condition)
            seen_conditions.add(condition)

        return {
            "tables": tables,
            "join_conditions": join_conditions,
        }

    @staticmethod
    def _extract_cte_names(expression: exp.Expression) -> set[str]:
        with_expression = expression.args.get("with")
        if with_expression is None:
            return set()

        cte_names: set[str] = set()
        for cte in with_expression.expressions:
            cte_name = str(cte.alias_or_name or "").strip()
            if cte_name:
                cte_names.add(cte_name.casefold())
        return cte_names

    @classmethod
    def _resolve_select_scope_info(
        cls,
        *,
        select_expression: exp.Select | None,
        cte_names: set[str],
        table_index_by_alias: dict[str, int],
        table_indexes_by_name: dict[str, list[int]],
        cache: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        if select_expression is None:
            return {
                "table_index_by_alias": table_index_by_alias,
                "table_indexes_by_name": table_indexes_by_name,
                "single_table_index": None,
            }

        cache_key = id(select_expression)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

        local_alias_map: dict[str, int] = {}
        local_name_map: dict[str, list[int]] = {}
        local_table_indexes: list[int] = []

        for source in cls._iter_select_scope_sources(select_expression):
            for table in cls._iter_scope_tables(source):
                table_name = cls._format_table_name(table)
                if not table_name or table_name.casefold() in cte_names:
                    continue

                matched_index = cls._resolve_table_index(
                    table_name=table_name,
                    table_alias=cls._extract_table_alias(table),
                    table_index_by_alias=table_index_by_alias,
                    table_indexes_by_name=table_indexes_by_name,
                )
                if matched_index is None:
                    continue

                if matched_index not in local_table_indexes:
                    local_table_indexes.append(matched_index)

                table_alias = cls._extract_table_alias(table)
                if table_alias:
                    local_alias_map[table_alias] = matched_index
                local_name_map.setdefault(table_name, [])
                if matched_index not in local_name_map[table_name]:
                    local_name_map[table_name].append(matched_index)

        scope_info = {
            "table_index_by_alias": local_alias_map,
            "table_indexes_by_name": local_name_map,
            "single_table_index": local_table_indexes[0] if len(local_table_indexes) == 1 else None,
            "explicit_select_aliases": cls._extract_explicit_select_aliases(select_expression),
        }
        cache[cache_key] = scope_info
        return scope_info

    @staticmethod
    def _normalize_sql(sql: str) -> str:
        if sql is None:
            raise ValueError("SQL text cannot be None.")

        normalized_sql = sql.strip()
        if not normalized_sql:
            raise ValueError("SQL text cannot be empty.")

        return normalized_sql

    @staticmethod
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

    @staticmethod
    def _extract_table_alias(table: exp.Table) -> str | None:
        alias = str(table.alias or "").strip()
        return alias or None

    @classmethod
    def _resolve_table_index(
        cls,
        *,
        table_name: str,
        table_alias: str | None,
        table_index_by_alias: dict[str, int],
        table_indexes_by_name: dict[str, list[int]],
    ) -> int | None:
        if table_alias:
            alias_match = table_index_by_alias.get(table_alias)
            if alias_match is not None:
                return alias_match

        table_name_matches = table_indexes_by_name.get(table_name) or []
        if len(table_name_matches) == 1:
            return table_name_matches[0]

        return None

    @staticmethod
    def _find_nearest_select(expression: exp.Expression) -> exp.Select | None:
        current = expression.parent
        while current is not None:
            if isinstance(current, exp.Select):
                return current
            current = current.parent
        return None

    @staticmethod
    def _iter_select_scope_sources(select_expression: exp.Select) -> list[exp.Expression]:
        sources: list[exp.Expression] = []

        from_expression = select_expression.args.get("from")
        if from_expression is not None:
            from_source = from_expression.this
            if isinstance(from_source, exp.Expression):
                sources.append(from_source)
            for extra_source in from_expression.args.get("expressions") or []:
                if isinstance(extra_source, exp.Expression):
                    sources.append(extra_source)

        for join in select_expression.args.get("joins") or []:
            join_source = getattr(join, "this", None)
            if isinstance(join_source, exp.Expression):
                sources.append(join_source)

        return sources

    @classmethod
    def _iter_scope_tables(cls, source: exp.Expression) -> list[exp.Table]:
        if isinstance(source, exp.Table):
            return [source]
        if isinstance(source, (exp.Subquery, exp.CTE)):
            return []

        tables: list[exp.Table] = []
        for child in source.iter_expressions():
            if isinstance(child, exp.Table):
                tables.append(child)
                continue
            if isinstance(child, (exp.Subquery, exp.CTE)):
                continue
            tables.extend(cls._iter_scope_tables(child))
        return tables

    @classmethod
    def _resolve_column_table_index(
        cls,
        *,
        column: exp.Column,
        scope_info: dict[str, Any],
    ) -> int | None:
        table_qualifier = str(column.table or "").strip()
        table_index_by_alias = dict(scope_info.get("table_index_by_alias") or {})
        table_indexes_by_name = dict(scope_info.get("table_indexes_by_name") or {})
        if table_qualifier:
            alias_match = table_index_by_alias.get(table_qualifier)
            if alias_match is not None:
                return alias_match

            table_name_matches = table_indexes_by_name.get(table_qualifier) or []
            if len(table_name_matches) == 1:
                return table_name_matches[0]
            return None

        single_table_index = scope_info.get("single_table_index")
        return single_table_index if isinstance(single_table_index, int) else None

    @staticmethod
    def _extract_explicit_select_aliases(select_expression: exp.Select) -> set[str]:
        aliases: set[str] = set()
        for select_item in select_expression.args.get("expressions") or []:
            alias = str(getattr(select_item, "alias", "") or "").strip()
            if alias:
                aliases.add(alias.casefold())
        return aliases

    @staticmethod
    def _is_explicit_select_alias_reference(
        *,
        column: exp.Column,
        scope_info: dict[str, Any],
    ) -> bool:
        if SqlglotParser._is_column_inside_alias_definition(column):
            return False

        # Only unqualified names can refer to SELECT output aliases.
        if str(column.table or "").strip():
            return False
        column_name = str(column.name or "").strip()
        if not column_name:
            return False
        explicit_aliases = scope_info.get("explicit_select_aliases") or set()
        return column_name.casefold() in explicit_aliases

    @staticmethod
    def _is_column_inside_alias_definition(column: exp.Column) -> bool:
        current = column
        while current is not None:
            parent = current.parent
            if parent is None:
                return False
            if isinstance(parent, exp.Alias):
                # The column is part of the alias expression itself (e.g. SELECT dept_id AS dept_id),
                # so it should still be counted as a source column.
                return parent.this is not None and current is not parent
            if isinstance(parent, exp.Select):
                return False
            current = parent
        return False

    @staticmethod
    def _extract_join_condition(join: exp.Join) -> str | None:
        on_expression = join.args.get("on")
        if on_expression is not None:
            condition = on_expression.sql()
            return condition.strip() or None

        using_expression = join.args.get("using")
        if using_expression is None:
            return None

        using_columns = []
        using_values = (
            using_expression
            if isinstance(using_expression, list)
            else getattr(using_expression, "expressions", []) or [using_expression]
        )
        for item in using_values:
            column_name = str(getattr(item, "name", None) or item.sql() or "").strip()
            if column_name:
                using_columns.append(column_name)

        if not using_columns:
            return None

        return f"USING ({', '.join(using_columns)})"

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
