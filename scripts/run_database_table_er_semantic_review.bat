@echo off
setlocal EnableExtensions EnableDelayedExpansion

pushd "%~dp0" || exit /b 1

set "PYTHON_EXE=D:\Tools\MiniConda\envs\spider2\python.exe"

@REM Existing NL2ER metadata batch. Each case should contain input.json and nl2er_output.json.
set "METADATA_DIR=D:\Workspace\Ace\Knowdata\metadata\20260507-120109"
set "METADATA_DIR=D:\Workspace\Ace\Knowdata\metadata\20260509-171854"

@REM Database snapshot group used by the question analysis. It should contain db_id subdirectories.
set "DATABASE_ROOT=D:\Workspace\ReFoRCE\spider2-snow\resource\databases"
set "DATABASE_ROOT=D:\Workspace\Ace\Knowdata\databases"

@REM Ground Truth Path 
set "GROUND_TRUTH_DIR=%CD%\data\ground_truth"
set "GROUND_TRUTH_DIR=%CD%\data\ground_truth_sy"

@REM Choose `single` to run specified question_id values, or `all` to traverse every question in METADATA_DIR.
set "RUN_MODE=single"

set "MAX_WORKERS=16"
set "MAX_TABLE_CONCURRENCY=64"
set "SAMPLE_ROW_LIMIT=2"
set "SAMPLE_VALUE_MAX_CHARS=300"
@REM Context table groups shown to each target table prompt: none, schema, or database.
set "TABLE_CONTEXT_SCOPE=schema"

@REM Multiple values can be comma-separated.
set "QUESTION_IDS=sy02"

set "LLM=deepseek_v4_flash"
@REM Supported values: non_think, think_high, think_max
set "REASONING_MODE=non_think"

@REM Logical model file inside each metadata case directory.
set "LOGICAL_MODEL_FILENAME=nl2er_output.json"
set "OUTPUT_FILENAME=database_table_er_semantic_review.json"
set "WRITE_SCHEMA_LINKING=1"
set "SCHEMA_LINKING_OUTPUT_FILENAME=schema_linking.json"
set "DRY_RUN=0"
set "LOG_ROOT=log\database_table_er_semantic_review"

set "RUN_REVIEW=1"
set "RUN_COVERAGE=1"

set "COVERAGE_SCHEMA_LINKING_FILENAME=%SCHEMA_LINKING_OUTPUT_FILENAME%"
set "COVERAGE_REPORT_FILENAME=database_table_er_schema_linking_coverage_report.txt"
set "COVERAGE_METADATA_DIR="
@REM set "COVERAGE_METADATA_DIR=D:\Workspace\Ace\Knowdata\log\database_table_er_semantic_review\20260509-165653"

@REM Optional. Leave empty to review every table group. Multiple values can be comma-separated.
set "TABLE_FULLNAMES="
@REM set "TABLE_FULLNAMES=BANK_SALES_TRADING.BANK_SALES_TRADING.BITCOIN_PRICES"

set "EXTRA_ARGS="
if not "%TABLE_FULLNAMES%"=="" set "EXTRA_ARGS=%EXTRA_ARGS% --table-fullname %TABLE_FULLNAMES%"
if "%WRITE_SCHEMA_LINKING%"=="1" set "EXTRA_ARGS=%EXTRA_ARGS% --write-schema-linking --schema-linking-output-filename %SCHEMA_LINKING_OUTPUT_FILENAME%"
if "%DRY_RUN%"=="1" set "EXTRA_ARGS=%EXTRA_ARGS% --dry-run"

if "%RUN_REVIEW%"=="1" (
  if /I "%RUN_MODE%"=="all" (
      "%PYTHON_EXE%" -B -m src.run.database_table_er_semantic_review --metadata-dir "%METADATA_DIR%" --database-root "%DATABASE_ROOT%" --log-root "%LOG_ROOT%" --logical-model-filename "%LOGICAL_MODEL_FILENAME%" --output-filename "%OUTPUT_FILENAME%" --table-context-scope "%TABLE_CONTEXT_SCOPE%" --model-config "%LLM%" --reasoning-mode "%REASONING_MODE%" --sample-row-limit %SAMPLE_ROW_LIMIT% --sample-value-max-chars %SAMPLE_VALUE_MAX_CHARS% --max-workers %MAX_WORKERS% --max-table-concurrency %MAX_TABLE_CONCURRENCY% %EXTRA_ARGS%
  ) else (
      "%PYTHON_EXE%" -B -m src.run.database_table_er_semantic_review --metadata-dir "%METADATA_DIR%" --database-root "%DATABASE_ROOT%" --log-root "%LOG_ROOT%" --logical-model-filename "%LOGICAL_MODEL_FILENAME%" --output-filename "%OUTPUT_FILENAME%" --question-id "%QUESTION_IDS%" --table-context-scope "%TABLE_CONTEXT_SCOPE%" --model-config "%LLM%" --reasoning-mode "%REASONING_MODE%" --sample-row-limit %SAMPLE_ROW_LIMIT% --sample-value-max-chars %SAMPLE_VALUE_MAX_CHARS% --max-workers %MAX_WORKERS% --max-table-concurrency %MAX_TABLE_CONCURRENCY% %EXTRA_ARGS%
  )

  if errorlevel 1 (
    echo [run_database_table_er_semantic_review.bat] database table ER semantic review run failed.
    popd
    exit /b 1
  )
)

if "%RUN_COVERAGE%"=="1" (
  if not "%WRITE_SCHEMA_LINKING%"=="1" (
    echo [run_database_table_er_semantic_review.bat] coverage skipped because WRITE_SCHEMA_LINKING is not 1.
  ) else (
    set "COVERAGE_TARGET_DIR=%COVERAGE_METADATA_DIR%"
    if "!COVERAGE_TARGET_DIR!"=="" (
      for /f "delims=" %%D in ('dir /b /ad /o-d "%LOG_ROOT%" 2^>nul') do (
        if "!COVERAGE_TARGET_DIR!"=="" set "COVERAGE_TARGET_DIR=%CD%\%LOG_ROOT%\%%D"
      )
    )
    if "!COVERAGE_TARGET_DIR!"=="" (
      echo [run_database_table_er_semantic_review.bat] coverage failed: no log run directory found.
      popd
      exit /b 1
    )

    "%PYTHON_EXE%" -B -m src.validation.schema_linking_coverage --metadata-dir "!COVERAGE_TARGET_DIR!" --ground-truth-dir "%GROUND_TRUTH_DIR%" --schema-linking-filename "%COVERAGE_SCHEMA_LINKING_FILENAME%" --output-path "!COVERAGE_TARGET_DIR!\%COVERAGE_REPORT_FILENAME%"

    if errorlevel 1 (
      echo [run_database_table_er_semantic_review.bat] schema linking coverage failed.
      popd
      exit /b 1
    )
  )
)

echo [run_database_table_er_semantic_review.bat] database table ER semantic review run completed.

popd
