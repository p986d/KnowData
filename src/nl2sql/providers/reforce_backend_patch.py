from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path
from typing import Any

try:
    import pymysql
except ModuleNotFoundError:
    pymysql = None
    DictCursor = None
else:
    from pymysql.cursors import DictCursor


def _quote_mysql_identifier(name: str) -> str:
    return f"`{str(name).replace('`', '``')}`"


def _normalize_mysql_table_full_name(full_name: str) -> str:
    parts = [part.strip() for part in str(full_name or "").split(".") if part.strip()]
    if len(parts) >= 3 and parts[0] == parts[1]:
        return ".".join([parts[0], parts[-1]])
    if len(parts) >= 2:
        return ".".join([parts[0], parts[-1]])
    return str(full_name or "").strip()


def _build_mysql_conn_kwargs(credentials: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": credentials["host"],
        "port": int(credentials["port"]),
        "user": credentials["user"],
        "password": credentials["password"],
        "database": credentials["database"],
        "charset": "utf8mb4",
        "autocommit": True,
        "cursorclass": DictCursor,
    }


def patch_schema_linking_upstream(
    upstream,
    *,
    backend: str,
) -> None:
    if backend != "mysql":
        return
    if hasattr(upstream, "SHORTLIST_PROMPT"):
        upstream.SHORTLIST_PROMPT = upstream.SHORTLIST_PROMPT.replace(
            "Snowflake database",
            "MySQL database",
        ).replace(
            "Snowflake db_id",
            "Database id",
        )


def patch_nl2sql_upstream(
    upstream,
    *,
    backend: str,
    dialect: str,
    mysql_credentials: dict[str, Any] | None,
) -> None:
    if backend != "mysql":
        return

    agent_module = sys.modules.get("agent")
    prompt_module = sys.modules.get("prompt")
    sql_module = sys.modules.get("sql")
    utils_module = sys.modules.get("utils")
    online_schema_linking_module = sys.modules.get("online_schema_linking")

    if agent_module is None or prompt_module is None or sql_module is None:
        raise RuntimeError("Failed to resolve upstream ReFoRCE support modules for MySQL patching.")

    original_agent_get_api_name = getattr(agent_module, "get_api_name")
    original_utils_get_api_name = getattr(utils_module, "get_api_name", None)
    original_prompts_cls = upstream.Prompts
    original_sql_env_cls = upstream.SqlEnv
    original_build_table_context = upstream.build_table_context
    original_build_snowflake_credentials = upstream.build_snowflake_credentials

    def _patched_get_api_name(sql_data):
        text = str(sql_data or "").strip().lower()
        if text.startswith("mysql_") or text.startswith("sf_online_") or text.startswith("mysql_online_"):
            return "mysql"
        return original_agent_get_api_name(sql_data)

    agent_module.get_api_name = _patched_get_api_name
    if original_utils_get_api_name is not None:
        utils_module.get_api_name = _patched_get_api_name

    if online_schema_linking_module is not None:
        patch_schema_linking_upstream(
            online_schema_linking_module,
            backend=backend,
        )

    class BackendAwarePrompts(original_prompts_cls):
        def get_prompt_dialect_list_all_tables(self, table_struct, api):
            if api == "mysql":
                return (
                    "When performing a UNION operation on many tables, explicitly list all tables. "
                    "Use executable MySQL table names only. Avoid placeholder omissions.\n"
                )
            return super().get_prompt_dialect_list_all_tables(table_struct, api)

        def get_prompt_dialect_nested(self, api):
            if api == "mysql":
                return (
                    "For JSON columns in MySQL, use JSON_EXTRACT or JSON_UNQUOTE(JSON_EXTRACT(...)). "
                    "Example: SELECT JSON_UNQUOTE(JSON_EXTRACT(t.json_col, '$.key')) AS key_value "
                    "FROM `database`.`table` AS t.\n"
                )
            return super().get_prompt_dialect_nested(api)

        def get_prompt_dialect_basic(self, api):
            if api == "mysql":
                return (
                    "```sql\nSELECT `column_name` FROM `database`.`table_name` WHERE ... ``` "
                    "(Replace with actual database and table names. Use backticks for identifiers.)"
                )
            return super().get_prompt_dialect_basic(api)

        def get_prompt_dialect_string_matching(self, api):
            if api == "mysql":
                return (
                    "For uncertain string matching, use case-insensitive LIKE via LOWER: "
                    "WHERE LOWER(col) LIKE LOWER('%target_str%').\n"
                )
            return super().get_prompt_dialect_string_matching(api)

    class BackendAwareSqlEnv(original_sql_env_cls):
        def __init__(self, snowflake_credentials=None, bigquery_credential_path=None):
            actual_mysql_credentials = None
            forward_snowflake_credentials = snowflake_credentials
            if isinstance(snowflake_credentials, dict) and snowflake_credentials.get("backend") == "mysql":
                actual_mysql_credentials = dict(snowflake_credentials)
                forward_snowflake_credentials = None

            super().__init__(
                snowflake_credentials=forward_snowflake_credentials,
                bigquery_credential_path=bigquery_credential_path,
            )
            self.mysql_credentials = actual_mysql_credentials or dict(mysql_credentials or {})

        def start_db_mysql(self, ex_id):
            if pymysql is None:
                raise ModuleNotFoundError("pymysql is required for MySQL execution")
            if ex_id not in self.conns:
                if not self.mysql_credentials:
                    raise ValueError("MySQL credentials are not configured for ReFoRCE execution.")
                self.conns[ex_id] = pymysql.connect(
                    **_build_mysql_conn_kwargs(self.mysql_credentials)
                )

        def exec_sql_mysql(self, sql_query, save_path, max_len, ex_id):
            cursor = self.conns[ex_id].cursor()
            try:
                cursor.execute(sql_query)
                column_info = cursor.description
                if column_info is None:
                    return "No data found for the specified query.\n"
                rows = self.get_rows(cursor, max_len)
                columns = [desc[0] for desc in column_info]
            except Exception as exc:
                return "##ERROR##" + str(exc)
            finally:
                try:
                    cursor.close()
                except Exception:
                    pass

            if not rows:
                return "No data found for the specified query.\n"

            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(columns)
            writer.writerows(
                [list(row.values()) if isinstance(row, dict) else list(row) for row in rows]
            )
            csv_content = output.getvalue()
            output.close()
            if save_path:
                with open(save_path, "w", newline="", encoding="utf-8") as file:
                    file.write(csv_content)
                return 0
            return csv_content[: int(max_len)] if max_len and len(csv_content) > int(max_len) else csv_content

        def execute_sql_api(
            self,
            sql_query,
            ex_id,
            save_path=None,
            api="sqlite",
            max_len=30000,
            sqlite_path=None,
            timeout=300,
        ):
            if api == "mysql":
                if ex_id not in self.conns:
                    self.start_db_mysql(ex_id)
                result = self.exec_sql_mysql(sql_query, save_path, max_len, ex_id)
                if "##ERROR##" in str(result):
                    return {"status": "error", "error_msg": str(result)}
                return str(result)
            return super().execute_sql_api(
                sql_query,
                ex_id,
                save_path=save_path,
                api=api,
                max_len=max_len,
                sqlite_path=sqlite_path,
                timeout=timeout,
            )

    def _patched_build_execution_credentials(request):
        if backend == "mysql":
            return {
                "backend": "mysql",
                "dialect": dialect,
                "host": mysql_credentials["host"],
                "port": mysql_credentials["port"],
                "user": mysql_credentials["user"],
                "password": mysql_credentials["password"],
                "database": mysql_credentials["database"],
            }
        return original_build_snowflake_credentials(request)

    def _patched_build_table_context(request, selected_groups, external_knowledge):
        if backend != "mysql":
            return original_build_table_context(request, selected_groups, external_knowledge)

        lines: list[str] = []
        table_structure: dict[str, list[str]] = {}

        for group, linked_columns in selected_groups:
            representative = group.representative
            normalized_full_name = _normalize_mysql_table_full_name(representative.full_name)
            parts = [part.strip() for part in normalized_full_name.split(".") if part.strip()]
            if len(parts) < 2:
                continue

            database = parts[0]
            table_names = [member.short_name for member in group.members]
            table_structure.setdefault(database, [])
            for table_name in table_names:
                if table_name not in table_structure[database]:
                    table_structure[database].append(table_name)

            lines.append("-" * 50)
            lines.append(f"Table full name: {normalized_full_name}")
            if len(group.members) > 1:
                lines.append(
                    f"This representative table stands for {len(group.members)} tables with the same schema."
                )
                lines.append(
                    "Some other tables have the similar structure: "
                    + str(
                        [
                            _normalize_mysql_table_full_name(member.full_name)
                            for member in group.members[1:]
                        ]
                    )
                )

            linked_set = {str(column).upper() for column in linked_columns}
            for column_name, column_type, description in zip(
                representative.column_names,
                representative.column_types,
                representative.descriptions,
            ):
                line = f"Column name: {column_name} Type: {column_type}"
                if str(column_name).upper() in linked_set:
                    line += " [schema-linked]"
                if description:
                    line += f" Description: {upstream.shorten_text(description, 240)}"
                lines.append(line)

            sample_rows = upstream.trim_sample_rows(
                representative.sample_rows,
                request.sample_row_limit,
                request.sample_value_max_chars,
            )
            if sample_rows:
                lines.append("Sample rows:")
                lines.append(json.dumps(sample_rows, ensure_ascii=False, indent=2))

        lines.append("")
        lines.append("External knowledge that might be helpful: ")
        lines.append(external_knowledge or "(none)")
        lines.append("")
        lines.append("The table structure information is ({database name: [table name]}): ")
        lines.append(str(table_structure))
        lines.append("")

        table_info = "\n".join(lines)
        table_struct = (
            "The table structure information is ({database name: [table name]}): \n"
            + str(table_structure)
            + "\n"
        )
        return table_info, table_struct, table_structure

    upstream.Prompts = BackendAwarePrompts
    if prompt_module is not None:
        prompt_module.Prompts = BackendAwarePrompts
    upstream.SqlEnv = BackendAwareSqlEnv
    if sql_module is not None:
        sql_module.SqlEnv = BackendAwareSqlEnv
    upstream.build_snowflake_credentials = _patched_build_execution_credentials
    upstream.build_table_context = _patched_build_table_context
    patch_schema_linking_upstream(
        upstream,
        backend=backend,
    )
