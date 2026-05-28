@echo off
setlocal EnableExtensions EnableDelayedExpansion

pushd "%~dp0" || exit /b 1

set "INPUT_PATH=%CD%\data\sy_input_with_hint.json"
set "RUN_MODE=all"

set "DATABASE_ROOT_SY=%CD%\databases"
set "SPIDER2_ROOT=D:\Workspace\ReFoRCE\spider2-snow"

set "MAX_WORKERS=16"
set "MAX_TABLE_CONCURRENCY=64"
set "SAMPLE_ROW_LIMIT=2"
set "SAMPLE_VALUE_MAX_CHARS=300"

set "QUESTION_IDS=sy23"

set "LLM=deepseek_v4_flash"
set "REASONING_MODE=non_think"

set "OUTPUT_FILENAME=database_table_profile.json"
set "WRITE_SCHEMA_LINKING=1"
set "SCHEMA_LINKING_OUTPUT_FILENAME=database_table_schema_linking.json"
set "DRY_RUN=0"
set "LOG_ROOT=log\database_table_semantic_sketch"

set "RUN_PROFILE=1"
set "RUN_COVERAGE=1"
set "GROUND_TRUTH_DIR=%CD%\data\ground_truth_sy"
set "COVERAGE_SCHEMA_LINKING_FILENAME=schema_linking.json"
set "COVERAGE_REPORT_FILENAME=schema_linking_coverage_report.txt"
set "COVERAGE_METADATA_DIR="
@REM set "COVERAGE_METADATA_DIR=D:\Workspace\Ace\Knowdata\log\database_table_semantic_sketch\20260508-140820\sf_bq050_20260507-120109"

set "TABLE_FULLNAMES="
@REM set "TABLE_FULLNAMES=PATENTSVIEW.PATENTSVIEW.PATENT"

set "EXTRA_ARGS="
if not "%TABLE_FULLNAMES%"=="" set "EXTRA_ARGS=%EXTRA_ARGS% --table-fullname %TABLE_FULLNAMES%"
if "%WRITE_SCHEMA_LINKING%"=="1" set "EXTRA_ARGS=%EXTRA_ARGS% --write-schema-linking --schema-linking-output-filename %SCHEMA_LINKING_OUTPUT_FILENAME%"
if "%DRY_RUN%"=="1" set "EXTRA_ARGS=%EXTRA_ARGS% --dry-run"

if "%RUN_PROFILE%"=="1" (
  if /I "%RUN_MODE%"=="all" (
      python -m src.run.database_table_semantic_sketch --input-path "%INPUT_PATH%" --database-root "%DATABASE_ROOT_SY%" --spider2-root "%SPIDER2_ROOT%" --log-root "%LOG_ROOT%" --output-filename "%OUTPUT_FILENAME%" --model-config "%LLM%" --reasoning-mode "%REASONING_MODE%" --sample-row-limit %SAMPLE_ROW_LIMIT% --sample-value-max-chars %SAMPLE_VALUE_MAX_CHARS% --max-workers %MAX_WORKERS% --max-table-concurrency %MAX_TABLE_CONCURRENCY% %EXTRA_ARGS%
  ) else (
      python -m src.run.database_table_semantic_sketch --input-path "%INPUT_PATH%" --database-root "%DATABASE_ROOT_SY%" --spider2-root "%SPIDER2_ROOT%" --log-root "%LOG_ROOT%" --output-filename "%OUTPUT_FILENAME%" --question-id "%QUESTION_IDS%" --model-config "%LLM%" --reasoning-mode "%REASONING_MODE%" --sample-row-limit %SAMPLE_ROW_LIMIT% --sample-value-max-chars %SAMPLE_VALUE_MAX_CHARS% --max-workers %MAX_WORKERS% --max-table-concurrency %MAX_TABLE_CONCURRENCY% %EXTRA_ARGS%
  )

  if errorlevel 1 (
    echo [run_database_table_profile.bat] database table profile run failed.
    popd
    exit /b 1
  )
)

if "%RUN_COVERAGE%"=="1" (
  if not "%WRITE_SCHEMA_LINKING%"=="1" (
    echo [run_database_table_profile.bat] coverage skipped because WRITE_SCHEMA_LINKING is not 1.
  ) else (
    set "COVERAGE_TARGET_DIR=%COVERAGE_METADATA_DIR%"
    if "!COVERAGE_TARGET_DIR!"=="" (
      for /f "delims=" %%D in ('dir /b /ad /o-d "%LOG_ROOT%" 2^>nul') do (
        if "!COVERAGE_TARGET_DIR!"=="" set "COVERAGE_TARGET_DIR=%CD%\%LOG_ROOT%\%%D"
      )
    )
    if "!COVERAGE_TARGET_DIR!"=="" (
      echo [run_database_table_profile.bat] coverage failed: no log run directory found.
      popd
      exit /b 1
    )

    python -m src.validation.schema_linking_coverage --metadata-dir "!COVERAGE_TARGET_DIR!" --ground-truth-dir "%GROUND_TRUTH_DIR%" --schema-linking-filename "%COVERAGE_SCHEMA_LINKING_FILENAME%" --output-path "!COVERAGE_TARGET_DIR!\%COVERAGE_REPORT_FILENAME%"

    if errorlevel 1 (
      echo [run_database_table_profile.bat] schema linking coverage failed.
      popd
      exit /b 1
    )
  )
)

echo [run_database_table_profile.bat] database table profile run completed.

popd
