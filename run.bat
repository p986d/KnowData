@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

@REM Choose `single` to run specified question_id values, or `all` to traverse every question in the input file.
set "RUN_MODE=all"

set "INPUT_PATH=%CD%\data\input.json"
@REM set "INPUT_PATH=%CD%\data\sy_input.json"
@REM set "INPUT_PATH=%CD%\data\sy_input_with_hint.json"

@REM Multiple values can be comma-separated.
set "QUESTION_IDS=00"

set "LLM=deepseek_v4_flash"
@REM Supported values: non_think, think_high, think_max
set "REASONING_MODE=non_think"

set "NL2SQL_ENGINE=reforce_gen_sl_m1"
set "MAX_BATCH_WORKERS=6"
set "MAX_LINKING_WORKERS=64"
set "MAX_NL2SQL_WORKERS=64"
set "NL2SQL_TIMEOUT_SECONDS=900"

if /I "%RUN_MODE%"=="all" (
    python -m src.run.pipeline ^
      --input-path "%INPUT_PATH%" ^
      --engine-provider "%NL2SQL_ENGINE%" ^
      --nl2er-model-config "%LLM%" ^
      --question-model-config "%LLM%" ^
      --schema-link-model-config "%LLM%" ^
      --nl2sql-model-config "%LLM%" ^
      --reasoning-mode "%REASONING_MODE%" ^
      --include-conditions-in-er2query ^
      --include-conditions-in-sql2nl ^
      --enable-nl2er-db-hint true ^
      --enable-er2data-db-hint true ^
      --enable-er2query-db-hint false ^
      --exclude-desc-in-er2query ^
      --max-batch-workers %MAX_BATCH_WORKERS% ^
      --max-linking-workers %MAX_LINKING_WORKERS% ^
      --nl2sql-timeout-seconds %NL2SQL_TIMEOUT_SECONDS% ^
      --max-nl2sql-workers %MAX_NL2SQL_WORKERS%
) else (
    python -m src.run.pipeline ^
      --input-path "%INPUT_PATH%" ^
      --question-id "%QUESTION_IDS%" ^
      --engine-provider "%NL2SQL_ENGINE%" ^
      --nl2er-model-config "%LLM%" ^
      --question-model-config "%LLM%" ^
      --schema-link-model-config "%LLM%" ^
      --nl2sql-model-config "%LLM%" ^
      --reasoning-mode "%REASONING_MODE%" ^
      --include-conditions-in-er2query ^
      --include-conditions-in-sql2nl ^
      --enable-nl2er-db-hint true ^
      --enable-er2data-db-hint true ^
      --enable-er2query-db-hint false ^
      --exclude-desc-in-er2query ^
      --max-batch-workers %MAX_BATCH_WORKERS% ^
      --max-linking-workers %MAX_LINKING_WORKERS% ^
      --nl2sql-timeout-seconds %NL2SQL_TIMEOUT_SECONDS% ^
      --max-nl2sql-workers %MAX_NL2SQL_WORKERS%
)

if errorlevel 1 (
    echo [run.bat] pipeline failed.
    popd
    exit /b 1
)

popd
