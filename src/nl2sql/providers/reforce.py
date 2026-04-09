from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from src.config import load_settings
from src.nl2sql.base import (
    EngineRuntimeConfig,
    NL2SQLRequest,
    NL2SQLResult,
    SchemaLinkingRequest,
    SchemaLinkingResult,
)
from src.nl2sql.defaults import (
    DEFAULT_NL2SQL_ENGINE_SCRIPT,
    DEFAULT_PROVIDER_LOG_ROOT,
    DEFAULT_REFORCE_ROOT,
    DEFAULT_SCHEMA_LINKING_ENGINE_SCRIPT,
    DEFAULT_SPIDER2_ROOT,
)


SCHEMA_LINKING_ENGINE_NAME = "reforce_online_schema_linking"
NL2SQL_ENGINE_NAME = "reforce_online_nl2sql"


def _load_json(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object at {file_path}, got {type(payload).__name__}")
    return payload


def _resolve_run_prefix(run_prefix: str | None) -> str:
    value = str(run_prefix or "").strip()
    if not value:
        raise ValueError("`run_prefix` is required when `output_path` is not provided.")
    return value


def _build_default_output_path(*, namespace: str, run_prefix: str, filename: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = DEFAULT_PROVIDER_LOG_ROOT / namespace / f"{run_prefix}_{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir / filename


def _resolve_output_path(
    output_path: str | None,
    *,
    namespace: str,
    run_prefix: str | None,
    filename: str,
) -> tuple[str, bool]:
    if output_path:
        path = Path(output_path).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        else:
            path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path), False

    return str(
        _build_default_output_path(
            namespace=namespace,
            run_prefix=_resolve_run_prefix(run_prefix),
            filename=filename,
        )
    ), False


def _configure_stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except ValueError:
            continue


def _should_echo_stdout_line(line: str) -> bool:
    text = line.strip()
    if not text:
        return False
    if "[online_nl2sql]" not in text:
        return False
    summary_markers = (
        "question=",
        "sql_generation_ok=",
        "error=",
        "stage=",
    )
    return any(marker in text for marker in summary_markers)


def _should_echo_stderr_line(line: str) -> bool:
    text = line.strip()
    if not text:
        return False
    keywords = ("Traceback", "Error", "Exception", "failed", "ModuleNotFoundError")
    return any(keyword in text for keyword in keywords)


def _stream_reader(stream, sink, buffer: list[str], prefix: str, echo_filter) -> None:
    try:
        for line in iter(stream.readline, ""):
            buffer.append(line)
            if echo_filter(line):
                sink.write(f"{prefix}{line}")
                sink.flush()
    finally:
        stream.close()


def _unique_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


def _parse_table_fqn(fullname: str) -> dict[str, str]:
    parts = [part.strip() for part in fullname.split(".") if part.strip()]
    if len(parts) < 3:
        return {"fullname": fullname}
    return {
        "fullname": fullname,
        "database": ".".join(parts[:-2]),
        "schema": parts[-2],
        "table": parts[-1],
    }


def _parse_column_fqn(fullname: str) -> dict[str, str]:
    parts = [part.strip() for part in fullname.split(".") if part.strip()]
    if len(parts) < 4:
        return {"fullname": fullname}
    return {
        "fullname": fullname,
        "database": ".".join(parts[:-3]),
        "schema": parts[-3],
        "table": parts[-2],
        "column": parts[-1],
    }


def _extract_items(payload: dict[str, Any], *candidate_keys: str) -> list[str]:
    parsed_info = payload.get("parsed_info")
    for source in (parsed_info, payload):
        if not isinstance(source, dict):
            continue
        for candidate_key in candidate_keys:
            items = _unique_strings(source.get(candidate_key, []))
            if items:
                return items
    return []


def _extract_schema_linking_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    nested_payload = payload.get("schema_linking")
    if isinstance(nested_payload, dict):
        return nested_payload
    return payload


def _extract_schema_linking_from_nl2sql_result(
    engine_result: dict[str, Any],
    *,
    fallback_result_path: str | None,
) -> tuple[list[dict[str, str]], list[dict[str, str]], str | None]:
    artifacts = engine_result.get("artifacts")
    schema_linking_artifact_path = None
    if isinstance(artifacts, dict):
        artifact_path = artifacts.get("schema_linking_path")
        if isinstance(artifact_path, str) and artifact_path.strip():
            schema_linking_artifact_path = artifact_path.strip()

    schema_linking_payload = _extract_schema_linking_payload(engine_result)
    if not schema_linking_payload and schema_linking_artifact_path:
        try:
            schema_linking_payload = _extract_schema_linking_payload(
                _load_json(schema_linking_artifact_path)
            )
        except Exception:
            schema_linking_payload = {}

    linked_tables = _extract_items(schema_linking_payload, "linked_tables", "gen_tb")
    linked_columns = _extract_items(schema_linking_payload, "linked_columns", "gen_col")
    tables = [_parse_table_fqn(fullname) for fullname in linked_tables]
    columns = [_parse_column_fqn(fullname) for fullname in linked_columns]
    return tables, columns, schema_linking_artifact_path or fallback_result_path


def _extract_engine_error(engine_result: dict[str, Any] | None) -> str | None:
    if not isinstance(engine_result, dict):
        return None
    raw_error = engine_result.get("error")
    if isinstance(raw_error, str):
        text = raw_error.strip()
        if text:
            return text
    return None


def _normalize_runtime(runtime: EngineRuntimeConfig | None) -> EngineRuntimeConfig:
    normalized = runtime or EngineRuntimeConfig(provider_name="reforce")
    return EngineRuntimeConfig(
        provider_name=normalized.provider_name or "reforce",
        root=Path(normalized.root or DEFAULT_REFORCE_ROOT),
        spider2_root=Path(normalized.spider2_root or DEFAULT_SPIDER2_ROOT),
        schema_linking_script=Path(
            normalized.schema_linking_script or DEFAULT_SCHEMA_LINKING_ENGINE_SCRIPT
        ),
        nl2sql_script=Path(normalized.nl2sql_script or DEFAULT_NL2SQL_ENGINE_SCRIPT),
        python_executable=normalized.python_executable or (sys.executable or "python"),
    )


class ReforceEngineProvider:
    def run_schema_linking(
        self,
        request: SchemaLinkingRequest,
        *,
        runtime: EngineRuntimeConfig | None = None,
        raise_on_error: bool = False,
    ) -> SchemaLinkingResult:
        runtime = _normalize_runtime(runtime)
        resolved_output_path, is_temporary_output = _resolve_output_path(
            request.output_path,
            namespace="schema_linking",
            run_prefix=request.run_prefix,
            filename="schema_linking_result.json",
        )
        command = [
            runtime.python_executable,
            str(runtime.schema_linking_script),
            "--api_key",
            request.api_key,
            "--base_url",
            request.base_url,
            "--model",
            request.model,
            "--db_id",
            request.db_id,
            "--question",
            request.question,
            "--spider2_root",
            str(runtime.spider2_root),
            "--output_path",
            resolved_output_path,
            "--temperature",
            str(request.temperature),
            "--shortlist_trigger",
            str(request.shortlist_trigger),
            "--max_shortlist_tables",
            str(request.max_shortlist_tables),
            "--sample_row_limit",
            str(request.sample_row_limit),
            "--sample_value_max_chars",
            str(request.sample_value_max_chars),
            "--similar_tables_hint_limit",
            str(request.similar_tables_hint_limit),
        ]

        try:
            completed = subprocess.run(
                command,
                cwd=runtime.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=request.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            result = SchemaLinkingResult(
                ok=False,
                engine=SCHEMA_LINKING_ENGINE_NAME,
                result_path=resolved_output_path,
                temporary_output=is_temporary_output,
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                exit_code=None,
                error=f"Schema linking timed out after {request.timeout_seconds} seconds.",
            )
            if raise_on_error:
                raise RuntimeError(result.error or "Schema linking timed out.")
            return result
        except Exception as exc:
            result = SchemaLinkingResult(
                ok=False,
                engine=SCHEMA_LINKING_ENGINE_NAME,
                result_path=resolved_output_path,
                temporary_output=is_temporary_output,
                stdout="",
                stderr="",
                exit_code=None,
                error=f"Failed to start schema linking process: {exc}",
            )
            if raise_on_error:
                raise RuntimeError(result.error or "Schema linking failed to start.") from exc
            return result

        if completed.returncode != 0:
            result = SchemaLinkingResult(
                ok=False,
                engine=SCHEMA_LINKING_ENGINE_NAME,
                result_path=resolved_output_path,
                temporary_output=is_temporary_output,
                stdout=completed.stdout,
                stderr=completed.stderr,
                exit_code=completed.returncode,
                error="Schema linking process failed.",
            )
            if raise_on_error:
                raise RuntimeError(
                    f"{result.error} exit_code={completed.returncode}\nSTDERR:\n{completed.stderr}"
                )
            return result

        output_file = Path(resolved_output_path)
        if not output_file.exists():
            result = SchemaLinkingResult(
                ok=False,
                engine=SCHEMA_LINKING_ENGINE_NAME,
                result_path=resolved_output_path,
                temporary_output=is_temporary_output,
                stdout=completed.stdout,
                stderr=completed.stderr,
                exit_code=completed.returncode,
                error=(
                    "Schema linking process succeeded but output file was not found: "
                    f"{resolved_output_path}"
                ),
            )
            if raise_on_error:
                raise RuntimeError(result.error or "Schema linking output missing.")
            return result

        try:
            engine_result = _load_json(output_file)
        except Exception as exc:
            result = SchemaLinkingResult(
                ok=False,
                engine=SCHEMA_LINKING_ENGINE_NAME,
                result_path=resolved_output_path,
                temporary_output=is_temporary_output,
                stdout=completed.stdout,
                stderr=completed.stderr,
                exit_code=completed.returncode,
                error=f"Failed to read schema linking result JSON: {exc}",
            )
            if raise_on_error:
                raise RuntimeError(result.error or "Schema linking output unreadable.") from exc
            return result

        tables = [
            _parse_table_fqn(fullname)
            for fullname in _extract_items(engine_result, "gen_tb", "linked_tables")
        ]
        columns = [
            _parse_column_fqn(fullname)
            for fullname in _extract_items(engine_result, "gen_col", "linked_columns")
        ]
        return SchemaLinkingResult(
            ok=True,
            engine=SCHEMA_LINKING_ENGINE_NAME,
            result_path=resolved_output_path,
            temporary_output=is_temporary_output,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
            raw_result=engine_result,
            tables=tables,
            columns=columns,
        )

    def run_nl2sql(
        self,
        request: NL2SQLRequest,
        *,
        runtime: EngineRuntimeConfig | None = None,
        raise_on_error: bool = False,
    ) -> NL2SQLResult:
        runtime = _normalize_runtime(runtime)
        resolved_output_path, is_temporary_output = _resolve_output_path(
            request.output_path,
            namespace="nl2sql",
            run_prefix=request.run_prefix,
            filename="nl2sql_result.json",
        )
        output_file = Path(resolved_output_path)
        external_knowledge_path = output_file.with_name(
            f"{output_file.stem}_external_knowledge.txt"
        )
        external_knowledge_path.write_text(
            request.external_knowledge,
            encoding="utf-8",
        )
        settings = load_settings()
        llm_config = settings.llm.get(request.llm_config_name)
        snowflake_config = settings.snowflake
        command = [
            runtime.python_executable,
            str(runtime.nl2sql_script),
            "--api_key",
            llm_config.api_key,
            "--base_url",
            llm_config.base_url,
            "--model",
            llm_config.model,
            "--db_id",
            request.db_id,
            "--question",
            request.question,
            "--external_knowledge_path",
            str(external_knowledge_path),
            "--spider2_root",
            str(runtime.spider2_root),
            "--output_path",
            resolved_output_path,
            "--temperature",
            str(request.temperature),
            "--schema_link_temperature",
            str(request.schema_link_temperature),
            "--num_votes",
            str(request.num_votes),
            "--max_workers",
            str(request.max_workers),
            "--max_iter",
            str(request.max_iter),
            "--timeout_seconds",
            str(request.timeout_seconds),
            "--snowflake_account",
            snowflake_config.account,
            "--snowflake_user",
            snowflake_config.user,
            "--snowflake_password",
            snowflake_config.password,
        ]
        if snowflake_config.role:
            command.extend(["--snowflake_role", snowflake_config.role])
        if snowflake_config.warehouse:
            command.extend(["--snowflake_warehouse", snowflake_config.warehouse])
        if request.generation_model:
            command.extend(["--generation_model", request.generation_model])
        if request.column_exploration_model:
            command.extend(["--column_exploration_model", request.column_exploration_model])

        _configure_stdio_utf8()
        print(
            f"[NL2SQL API] starting request db_id={request.db_id} "
            f"engine_script={runtime.nl2sql_script}",
            flush=True,
        )
        print(
            f"[NL2SQL API] output_path={resolved_output_path} "
            f"temporary_output={is_temporary_output}",
            flush=True,
        )

        try:
            child_env = os.environ.copy()
            child_env.setdefault("PYTHONIOENCODING", "utf-8")
            child_env.setdefault("PYTHONUTF8", "1")
            process = subprocess.Popen(
                command,
                cwd=runtime.root,
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            print(f"[NL2SQL API] subprocess started pid={process.pid}", flush=True)

            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            stdout_thread = threading.Thread(
                target=_stream_reader,
                args=(process.stdout, sys.stdout, stdout_chunks, "[ReFoRCE] ", _should_echo_stdout_line),
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=_stream_reader,
                args=(process.stderr, sys.stderr, stderr_chunks, "[ReFoRCE][stderr] ", _should_echo_stderr_line),
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()

            try:
                returncode = process.wait(timeout=request.timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout_thread.join(timeout=2)
                stderr_thread.join(timeout=2)
                raise

            stdout_thread.join()
            stderr_thread.join()
            completed_stdout = "".join(stdout_chunks)
            completed_stderr = "".join(stderr_chunks)
            print(f"[NL2SQL API] subprocess finished exit_code={returncode}", flush=True)
        except subprocess.TimeoutExpired as exc:
            result = NL2SQLResult(
                ok=False,
                engine=NL2SQL_ENGINE_NAME,
                result_path=resolved_output_path,
                temporary_output=is_temporary_output,
                stdout=(exc.stdout or "") if hasattr(exc, "stdout") else "",
                stderr=(exc.stderr or "") if hasattr(exc, "stderr") else "",
                exit_code=None,
                error=f"NL2SQL process timed out after {request.timeout_seconds} seconds.",
            )
            if raise_on_error:
                raise RuntimeError(result.error or "NL2SQL timed out.")
            return result
        except Exception as exc:
            result = NL2SQLResult(
                ok=False,
                engine=NL2SQL_ENGINE_NAME,
                result_path=resolved_output_path,
                temporary_output=is_temporary_output,
                stdout="",
                stderr="",
                exit_code=None,
                error=f"Failed to start NL2SQL process: {exc}",
            )
            if raise_on_error:
                raise RuntimeError(result.error or "NL2SQL failed to start.") from exc
            return result

        if returncode != 0 and output_file.exists():
            try:
                engine_result = _load_json(output_file)
            except Exception:
                engine_result = None
            else:
                final_result = (
                    engine_result.get("final", {})
                    if isinstance(engine_result, dict)
                    else {}
                )
                sql = str(final_result.get("sql") or "")
                engine_error = _extract_engine_error(engine_result)
                tables, columns, schema_linking_result_path = _extract_schema_linking_from_nl2sql_result(
                    engine_result,
                    fallback_result_path=resolved_output_path,
                )
                return NL2SQLResult(
                    ok=bool(sql),
                    engine=NL2SQL_ENGINE_NAME,
                    result_path=resolved_output_path,
                    temporary_output=is_temporary_output,
                    stdout=completed_stdout,
                    stderr=completed_stderr,
                    exit_code=returncode,
                    sql=sql,
                    error=None if sql else engine_error,
                    raw_result=engine_result,
                    tables=tables,
                    columns=columns,
                    schema_linking_result_path=schema_linking_result_path,
                    warning=(
                        "NL2SQL process exited with a non-zero code, "
                        "but a readable result JSON was recovered from disk."
                    ),
                )

        if returncode != 0:
            result = NL2SQLResult(
                ok=False,
                engine=NL2SQL_ENGINE_NAME,
                result_path=resolved_output_path,
                temporary_output=is_temporary_output,
                stdout=completed_stdout,
                stderr=completed_stderr,
                exit_code=returncode,
                error="NL2SQL process failed.",
            )
            if raise_on_error:
                raise RuntimeError(
                    f"{result.error} exit_code={returncode}\nSTDERR:\n{completed_stderr}"
                )
            return result

        if not output_file.exists():
            result = NL2SQLResult(
                ok=False,
                engine=NL2SQL_ENGINE_NAME,
                result_path=resolved_output_path,
                temporary_output=is_temporary_output,
                stdout=completed_stdout,
                stderr=completed_stderr,
                exit_code=returncode,
                error=f"NL2SQL process succeeded but output file was not found: {resolved_output_path}",
            )
            if raise_on_error:
                raise RuntimeError(result.error or "NL2SQL output missing.")
            return result

        try:
            engine_result = _load_json(output_file)
        except Exception as exc:
            result = NL2SQLResult(
                ok=False,
                engine=NL2SQL_ENGINE_NAME,
                result_path=resolved_output_path,
                temporary_output=is_temporary_output,
                stdout=completed_stdout,
                stderr=completed_stderr,
                exit_code=returncode,
                error=f"Failed to read NL2SQL result JSON: {exc}",
            )
            if raise_on_error:
                raise RuntimeError(result.error or "NL2SQL output unreadable.") from exc
            return result

        final_result = engine_result.get("final", {}) if isinstance(engine_result, dict) else {}
        sql = str(final_result.get("sql") or "")
        engine_error = _extract_engine_error(engine_result)
        tables, columns, schema_linking_result_path = _extract_schema_linking_from_nl2sql_result(
            engine_result,
            fallback_result_path=resolved_output_path,
        )
        return NL2SQLResult(
            ok=bool(sql),
            engine=NL2SQL_ENGINE_NAME,
            result_path=resolved_output_path,
            temporary_output=is_temporary_output,
            stdout=completed_stdout,
            stderr=completed_stderr,
            exit_code=returncode,
            sql=sql,
            error=None if sql else engine_error,
            raw_result=engine_result,
            tables=tables,
            columns=columns,
            schema_linking_result_path=schema_linking_result_path,
        )
