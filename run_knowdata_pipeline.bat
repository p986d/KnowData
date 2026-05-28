@echo off
setlocal EnableExtensions EnableDelayedExpansion

pushd "%~dp0" || exit /b 1

@REM Choose `single` to run specified question_id values, or `all` to traverse every question in INPUT_PATH.
set "RUN_MODE=all"


set "INPUT_PATH=%CD%\data\input.json"

set "GROUND_TRUTH_DIR=%CD%\data\ground_truth_sy"

@REM set "INPUT_PATH=%CD%\data\sy_input.json"
set "INPUT_PATH=%CD%\data\sy_input_with_hint.json"
@REM Multiple values can be comma-separated.
@REM set "QUESTION_IDS=sf_bq052"

set "METADATA_ROOT=%CD%\metadata"
set "LOG_ROOT=%CD%\log\knowdata_pipeline"
set "MAX_STAGE_WORKERS=24"

set "LLM=deepseek_v4_flash"
set "ER2DATA_LLM=deepseek_chat"
@REM Supported values: non_think, think_high, think_max
set "REASONING_MODE=non_think"

set "NL2ER_PROMPT_TEMPLATE_NAME=NL2ER_ERA_sketch_st1_v0.6.md"
set "DIFF_IDEA_COUNT=4"
set "DIFF_MAX_RETRY=2"
set "INCLUDE_RESOLVE_PROCESS=1"

set "ENGINE_PROVIDER=reforce_gen_sl_m1"
set "ER2QUERY_TEMPLATE_NAME=ER2Data_er2query_st1_v1.2.md"
set "MAX_QUESTION_CONCURRENCY=24"
set "MAX_CANDIDATE_WORKERS=24"
set "CANDIDATE_TIMEOUT_SECONDS=900"
set "NUM_VOTES=2"
set "REFORCE_MAX_WORKERS=24"
set "MAX_ITER=5"
set "ENABLE_NL2SQL_DB_HINT=true"
set "ENABLE_ER2QUERY_DB_HINT=false"
set "EXCLUDE_DESC_IN_ER2QUERY=false"
set "INCLUDE_CONDITIONS_IN_ER2QUERY=true"

set "ER_SELECTION_PROMPT_TEMPLATE_NAME=ER2Data_selection_test_v0.md"
set "INCLUDE_RELATION_CANDIDATES=true"
set "ER_SELECTION_SAMPLE_ROW_LIMIT=2"
set "ER_SELECTION_SAMPLE_VALUE_MAX_CHARS=160"

set "RUN_COVERAGE=1"
set "COVERAGE_SHOW_COVERED_DETAILS=1"

for /f "usebackq delims=" %%T in (`python -c "from src.utils.run_log import build_timestamp; print(build_timestamp())"`) do (
  if not defined RUN_TIMESTAMP set "RUN_TIMESTAMP=%%T"
)
if not defined RUN_TIMESTAMP (
  echo [run_knowdata_pipeline.bat] failed to create RUN_TIMESTAMP.
  popd
  exit /b 1
)

set "METADATA_DIR=%METADATA_ROOT%\%RUN_TIMESTAMP%"
set "RUN_LOG_DIR=%LOG_ROOT%\%RUN_TIMESTAMP%"

set "RESOLVE_PROCESS_ARG="
if "%INCLUDE_RESOLVE_PROCESS%"=="1" set "RESOLVE_PROCESS_ARG=--include-resolve-process"

set "COVERAGE_DETAILS_ARG="
if "%COVERAGE_SHOW_COVERED_DETAILS%"=="1" set "COVERAGE_DETAILS_ARG=--show-covered-details"

@REM if /I "%RUN_MODE%"=="all" (
@REM   python -B -m src.run.knowdata_pipeline ^
@REM     --input-path "%INPUT_PATH%" ^
@REM     --metadata-root "%METADATA_ROOT%" ^
@REM     --log-root "%LOG_ROOT%" ^
@REM     --timestamp "%RUN_TIMESTAMP%" ^
@REM     --max-stage-workers %MAX_STAGE_WORKERS% ^
@REM     --reasoning-mode "%REASONING_MODE%" ^
@REM     --nl2er-prompt-template-name "%NL2ER_PROMPT_TEMPLATE_NAME%" ^
@REM     --nl2er-model-config "%LLM%" ^
@REM     --diff-idea-count %DIFF_IDEA_COUNT% ^
@REM     --diff-max-retry %DIFF_MAX_RETRY% ^
@REM     %RESOLVE_PROCESS_ARG% ^
@REM     --engine-provider "%ENGINE_PROVIDER%" ^
@REM     --er2query-template-name "%ER2QUERY_TEMPLATE_NAME%" ^
@REM     --er2data-question-model-config "%ER2DATA_LLM%" ^
@REM     --er2data-candidate-model-config "%ER2DATA_LLM%" ^
@REM     --max-question-concurrency %MAX_QUESTION_CONCURRENCY% ^
@REM     --max-candidate-workers %MAX_CANDIDATE_WORKERS% ^
@REM     --candidate-timeout-seconds %CANDIDATE_TIMEOUT_SECONDS% ^
@REM     --num-votes %NUM_VOTES% ^
@REM     --reforce-max-workers %REFORCE_MAX_WORKERS% ^
@REM     --max-iter %MAX_ITER% ^
@REM     --enable-nl2sql-db-hint %ENABLE_NL2SQL_DB_HINT% ^
@REM     --enable-er2query-db-hint %ENABLE_ER2QUERY_DB_HINT% ^
@REM     --exclude-desc-in-er2query %EXCLUDE_DESC_IN_ER2QUERY% ^
@REM     --include-conditions-in-er2query %INCLUDE_CONDITIONS_IN_ER2QUERY% ^
@REM     --er-selection-prompt-template-name "%ER_SELECTION_PROMPT_TEMPLATE_NAME%" ^
@REM     --er-selection-model-config "%LLM%" ^
@REM     --include-relation-candidates %INCLUDE_RELATION_CANDIDATES% ^
@REM     --er-selection-sample-row-limit %ER_SELECTION_SAMPLE_ROW_LIMIT% ^
@REM     --er-selection-sample-value-max-chars %ER_SELECTION_SAMPLE_VALUE_MAX_CHARS%
@REM   if errorlevel 1 (
@REM     echo [run_knowdata_pipeline.bat] pipeline all run failed.
@REM     popd
@REM     exit /b 1
@REM   )

@REM   call :run_coverage
@REM   if errorlevel 1 (
@REM     echo [run_knowdata_pipeline.bat] schema linking coverage failed.
@REM     popd
@REM     exit /b 1
@REM   )

@REM   popd
@REM   exit /b 0
@REM )

@REM if /I "%RUN_MODE%"=="single" (
@REM   python -B -m src.run.knowdata_pipeline ^
@REM     --input-path "%INPUT_PATH%" ^
@REM     --question-id "%QUESTION_IDS%" ^
@REM     --metadata-root "%METADATA_ROOT%" ^
@REM     --log-root "%LOG_ROOT%" ^
@REM     --timestamp "%RUN_TIMESTAMP%" ^
@REM     --max-stage-workers %MAX_STAGE_WORKERS% ^
@REM     --reasoning-mode "%REASONING_MODE%" ^
@REM     --nl2er-prompt-template-name "%NL2ER_PROMPT_TEMPLATE_NAME%" ^
@REM     --nl2er-model-config "%LLM%" ^
@REM     --diff-idea-count %DIFF_IDEA_COUNT% ^
@REM     --diff-max-retry %DIFF_MAX_RETRY% ^
@REM     %RESOLVE_PROCESS_ARG% ^
@REM     --engine-provider "%ENGINE_PROVIDER%" ^
@REM     --er2query-template-name "%ER2QUERY_TEMPLATE_NAME%" ^
@REM     --er2data-question-model-config "%ER2DATA_LLM%" ^
@REM     --er2data-candidate-model-config "%ER2DATA_LLM%" ^
@REM     --max-question-concurrency %MAX_QUESTION_CONCURRENCY% ^
@REM     --max-candidate-workers %MAX_CANDIDATE_WORKERS% ^
@REM     --candidate-timeout-seconds %CANDIDATE_TIMEOUT_SECONDS% ^
@REM     --num-votes %NUM_VOTES% ^
@REM     --reforce-max-workers %REFORCE_MAX_WORKERS% ^
@REM     --max-iter %MAX_ITER% ^
@REM     --enable-nl2sql-db-hint %ENABLE_NL2SQL_DB_HINT% ^
@REM     --enable-er2query-db-hint %ENABLE_ER2QUERY_DB_HINT% ^
@REM     --exclude-desc-in-er2query %EXCLUDE_DESC_IN_ER2QUERY% ^
@REM     --include-conditions-in-er2query %INCLUDE_CONDITIONS_IN_ER2QUERY% ^
@REM     --er-selection-prompt-template-name "%ER_SELECTION_PROMPT_TEMPLATE_NAME%" ^
@REM     --er-selection-model-config "%LLM%" ^
@REM     --include-relation-candidates %INCLUDE_RELATION_CANDIDATES% ^
@REM     --er-selection-sample-row-limit %ER_SELECTION_SAMPLE_ROW_LIMIT% ^
@REM     --er-selection-sample-value-max-chars %ER_SELECTION_SAMPLE_VALUE_MAX_CHARS%
@REM   if errorlevel 1 (
@REM     echo [run_knowdata_pipeline.bat] pipeline single run failed.
@REM     popd
@REM     exit /b 1
@REM   )

@REM   call :run_coverage
@REM   if errorlevel 1 (
@REM     echo [run_knowdata_pipeline.bat] schema linking coverage failed.
@REM     popd
@REM     exit /b 1
@REM   )

@REM   popd
@REM   exit /b 0
@REM )

@REM echo [run_knowdata_pipeline.bat] unknown RUN_MODE: %RUN_MODE%
@REM echo [run_knowdata_pipeline.bat] supported values: single, all
@REM popd
@REM exit /b 1

:run_coverage
if not "%RUN_COVERAGE%"=="1" (
  echo [run_knowdata_pipeline.bat] schema linking coverage skipped.
  exit /b 0
)

if not exist "%GROUND_TRUTH_DIR%" (
  echo [run_knowdata_pipeline.bat] schema linking coverage skipped: ground truth dir not found: %GROUND_TRUTH_DIR%
  exit /b 0
)

set "METADATA_DIR=D:\Workspace\Ace\Knowdata\metadata\20260523-013119"

python -B -m src.validation.schema_linking_coverage ^
  --metadata-dir "%METADATA_DIR%" ^
  --ground-truth-dir "%GROUND_TRUTH_DIR%" ^
  --schema-linking-filename "schema_linking.json" ^
  --output-path "%RUN_LOG_DIR%\schema_linking_coverage_report.txt" %COVERAGE_DETAILS_ARG%
if errorlevel 1 exit /b 1

python -B -m src.validation.schema_linking_coverage ^
  --metadata-dir "%METADATA_DIR%" ^
  --ground-truth-dir "%GROUND_TRUTH_DIR%" ^
  --schema-linking-filename "schema_linking.json" ^
  --output-path "%RUN_LOG_DIR%\schema_linking_coverage_report.json" ^
  --json
if errorlevel 1 exit /b 1

exit /b 0
