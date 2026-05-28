@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

@REM Existing metadata batch. Each case should contain input.json and the schema-linking file below.
set "METADATA_DIR=D:\Workspace\Ace\Knowdata\metadata\20260507-120109"
@REM Choose `single` to run specified question_id values, or `all` to traverse every question in METADATA_DIR.
set "RUN_MODE=all"
set "MAX_WORKERS=16"
set "MAX_TABLE_CONCURRENCY=64"
set "SAMPLE_ROW_LIMIT=2"
set "SAMPLE_VALUE_MAX_CHARS=300"

@REM Multiple values can be comma-separated.
set "QUESTION_IDS=sf_local015"

set "LLM=deepseek_v4_flash"
@REM Supported values: non_think, think_high, think_max
set "REASONING_MODE=non_think"

@REM Input schema-linking file inside each metadata case directory.
set "SCHEMA_LINKING_FILENAME=schema_linking.json"
set "OUTPUT_FILENAME=linked_table_profile.json"

@REM Optional. Leave empty to profile every linked table. Multiple values can be comma-separated.
set "TABLE_FULLNAMES="
@REM set "TABLE_FULLNAMES=PATENTSVIEW.PATENTSVIEW.PATENT"

set "EXTRA_ARGS="
if not "%TABLE_FULLNAMES%"=="" set "EXTRA_ARGS=--table-fullname %TABLE_FULLNAMES%"

if /I "%RUN_MODE%"=="all" (
    python -m src.run.schema_linking_table_semantic_sketch --metadata-dir "%METADATA_DIR%" --schema-linking-filename "%SCHEMA_LINKING_FILENAME%" --output-filename "%OUTPUT_FILENAME%" --model-config "%LLM%" --reasoning-mode "%REASONING_MODE%" --sample-row-limit %SAMPLE_ROW_LIMIT% --sample-value-max-chars %SAMPLE_VALUE_MAX_CHARS% --max-workers %MAX_WORKERS% --max-table-concurrency %MAX_TABLE_CONCURRENCY% %EXTRA_ARGS%
) else (
    python -m src.run.schema_linking_table_semantic_sketch --metadata-dir "%METADATA_DIR%" --schema-linking-filename "%SCHEMA_LINKING_FILENAME%" --output-filename "%OUTPUT_FILENAME%" --question-id "%QUESTION_IDS%" --model-config "%LLM%" --reasoning-mode "%REASONING_MODE%" --sample-row-limit %SAMPLE_ROW_LIMIT% --sample-value-max-chars %SAMPLE_VALUE_MAX_CHARS% --max-workers %MAX_WORKERS% --max-table-concurrency %MAX_TABLE_CONCURRENCY% %EXTRA_ARGS%
)

if errorlevel 1 (
  echo [run_linked_table_profile.bat] linked table profile run failed.
  popd
  exit /b 1
)

popd
