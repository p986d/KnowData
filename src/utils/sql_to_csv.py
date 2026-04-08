from __future__ import annotations

import argparse
import codecs
import csv
import os
import re
from pathlib import Path
from typing import Sequence

from src.utils.run_log import build_timestamp


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "src" / "config" / "base.yaml"
DEFAULT_OUTPUT_ROOT = Path("metadata") / "sql_to_csv"
DEFAULT_ROW_LIMIT = 100
_UNICODE_ESCAPE_RE = re.compile(r"\\u[0-9a-fA-F]{4}|\\x[0-9a-fA-F]{2}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Execute a Snowflake SQL string and save the result set to CSV. "
            "Supports escaped SQL text such as \\n and \\\"."
        )
    )
    parser.add_argument(
        "sql",
        nargs="?",
        help="SQL string. You can pass escaped text such as SELECT\\n1 AS col.",
    )
    parser.add_argument(
        "--sql",
        dest="sql_option",
        default=None,
        help="SQL string. Equivalent to the positional `sql` argument.",
    )
    parser.add_argument(
        "--sql-from-env",
        default=None,
        help="Read the raw SQL string from an environment variable.",
    )
    parser.add_argument(
        "--sql-file",
        type=Path,
        default=None,
        help="Read SQL text from a file. Useful for multi-line SQL.",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="Read SQL text from standard input. Useful with pipe/redirection.",
    )
    parser.add_argument(
        "--output-path",
        type=Path,
        default=None,
        help="Target CSV path. Defaults to metadata/sql_to_csv/query_result_<timestamp>.csv",
    )
    parser.add_argument(
        "--row-limit",
        type=int,
        default=DEFAULT_ROW_LIMIT,
        help=f"Maximum number of rows to write. Default: {DEFAULT_ROW_LIMIT}",
    )
    parser.add_argument(
        "--env-path",
        type=Path,
        default=DEFAULT_ENV_PATH,
        help=f"Path to the .env file. Default: {DEFAULT_ENV_PATH}",
    )
    parser.add_argument(
        "--config-path",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to the YAML config file. Default: {DEFAULT_CONFIG_PATH}",
    )
    return parser.parse_args()


def _strip_matching_quotes(text: str) -> str:
    stripped = text.strip()
    while len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ("'", '"'):
        stripped = stripped[1:-1].strip()
    return stripped


def decode_sql_text(raw_sql: str) -> str:
    text = _strip_matching_quotes(raw_sql)

    replacements: Sequence[tuple[str, str]] = (
        ("\\r\\n", "\n"),
        ("\\n", "\n"),
        ("\\r", "\r"),
        ("\\t", "\t"),
        ('\\"', '"'),
        ("\\'", "'"),
    )
    for needle, replacement in replacements:
        text = text.replace(needle, replacement)

    if _UNICODE_ESCAPE_RE.search(text):
        try:
            text = codecs.decode(text, "unicode_escape")
        except UnicodeDecodeError:
            pass

    return text.strip()


def resolve_sql_input(args: argparse.Namespace) -> str:
    if args.sql_file is not None:
        sql_path = args.sql_file.expanduser()
        if not sql_path.is_absolute():
            sql_path = (Path.cwd() / sql_path).resolve()
        else:
            sql_path = sql_path.resolve()
        if not sql_path.exists():
            raise FileNotFoundError(f"SQL file not found: {sql_path}")
        sql = sql_path.read_text(encoding="utf-8")
    elif args.stdin:
        import sys

        sql = sys.stdin.read()
    elif args.sql_from_env:
        env_name = str(args.sql_from_env).strip()
        if not env_name:
            raise ValueError("`--sql-from-env` cannot be empty.")
        if env_name not in os.environ:
            raise KeyError(f"Environment variable not found: {env_name}")
        raw_sql = os.environ[env_name]
        sql = decode_sql_text(str(raw_sql))
    else:
        raw_sql = args.sql_option if args.sql_option is not None else args.sql
        if raw_sql is None:
            raise ValueError(
                "SQL input is required. Pass a positional SQL string, --sql, --sql-from-env, --sql-file, or --stdin."
            )
        sql = decode_sql_text(str(raw_sql))

    if not sql:
        raise ValueError("Resolved SQL text is empty.")
    return sql


def resolve_output_path(output_path: Path | None) -> Path:
    if output_path is not None:
        resolved = output_path.expanduser()
        if not resolved.is_absolute():
            resolved = (Path.cwd() / resolved).resolve()
        else:
            resolved = resolved.resolve()
    else:
        resolved = (Path.cwd() / DEFAULT_OUTPUT_ROOT / f"query_result_{build_timestamp()}.csv").resolve()

    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _strip_inline_comment(text: str) -> str:
    in_single = False
    in_double = False
    result: list[str] = []

    for idx, ch in enumerate(text):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            break
        result.append(ch)

    return "".join(result).strip()


def _parse_scalar(text: str) -> str | None:
    value = _strip_inline_comment(text).strip()
    if not value:
        return ""
    if value.lower() in {"null", "none"}:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def load_env_file(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not env_path.exists():
        return values

    with env_path.open("r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            normalized_key = key.strip()
            if not normalized_key:
                continue
            values[normalized_key] = _parse_scalar(value) or ""
    return values


def load_snowflake_yaml_section(config_path: Path) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    if not config_path.exists():
        return result

    in_snowflake_section = False
    with config_path.open("r", encoding="utf-8") as config_file:
        for raw_line in config_file:
            line = raw_line.rstrip("\r\n")
            if not line.strip():
                continue

            stripped = line.lstrip()
            indent = len(line) - len(stripped)

            if indent == 0 and stripped == "snowflake:":
                in_snowflake_section = True
                continue

            if in_snowflake_section and indent == 0:
                break

            if not in_snowflake_section or indent < 2 or ":" not in stripped:
                continue

            key, value = stripped.split(":", 1)
            result[key.strip()] = _parse_scalar(value)

    return result


def build_snowflake_connection_kwargs(
    *,
    env_path: Path,
    config_path: Path,
) -> dict[str, str]:
    env_values = load_env_file(env_path)
    yaml_values = load_snowflake_yaml_section(config_path)

    def _require_env(key: str) -> str:
        value = os.environ.get(key)
        if value is None or not value.strip():
            value = env_values.get(key)
        if value is None or not str(value).strip():
            raise KeyError(f"Required Snowflake credential not found: {key}")
        return str(value).strip()

    conn_kwargs: dict[str, str] = {
        "account": _require_env("SNOWFLAKE_ACCOUNT"),
        "user": _require_env("SNOWFLAKE_USER"),
        "password": _require_env("SNOWFLAKE_PASSWORD"),
    }

    for optional_key in ("warehouse", "role", "database", "schema"):
        value = yaml_values.get(optional_key)
        if value is not None and str(value).strip():
            conn_kwargs[optional_key] = str(value).strip()

    return conn_kwargs


def fetch_rows_to_csv(
    *,
    sql: str,
    output_path: Path,
    row_limit: int,
    env_path: Path,
    config_path: Path,
) -> tuple[int, bool]:
    import snowflake.connector

    conn = snowflake.connector.connect(
        **build_snowflake_connection_kwargs(
            env_path=env_path,
            config_path=config_path,
        )
    )
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            if cursor.description is None:
                raise ValueError("The SQL executed successfully but did not return a result set.")

            column_names = [desc[0] for desc in cursor.description]
            rows = cursor.fetchmany(max(1, row_limit) + 1)
        finally:
            cursor.close()
    finally:
        conn.close()

    truncated = len(rows) > row_limit
    rows_to_write = rows[:row_limit]

    with output_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(column_names)
        for row in rows_to_write:
            writer.writerow(list(row))

    return len(rows_to_write), truncated


def main() -> None:
    args = parse_args()
    if args.row_limit <= 0:
        raise ValueError("`--row-limit` must be a positive integer.")

    sql = resolve_sql_input(args)
    output_path = resolve_output_path(args.output_path)
    written_rows, truncated = fetch_rows_to_csv(
        sql=sql,
        output_path=output_path,
        row_limit=args.row_limit,
        env_path=args.env_path,
        config_path=args.config_path,
    )

    print(f"[sql_to_csv] output_path={output_path}")
    print(f"[sql_to_csv] written_rows={written_rows}")
    print(f"[sql_to_csv] truncated={str(truncated)}")


if __name__ == "__main__":
    main()
