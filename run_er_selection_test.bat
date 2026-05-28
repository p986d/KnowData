@echo off
setlocal EnableExtensions EnableDelayedExpansion

pushd "%~dp0" || exit /b 1

@REM Choose `single` to run one case directory, or `all` to traverse every case under METADATA_DIR.
set "RUN_MODE=single"

@REM Pass a case directory as the first argument for single mode, or edit CASE_DIR below.
set "CASE_DIR=%~1"
if "%CASE_DIR%"=="" set "CASE_DIR=%CD%\metadata\20260507-120109\sf_bq052_20260507-120109"

@REM Used by all mode. The script runs child directories containing nl2er_output.json and schema_linking.json.
set "METADATA_DIR=%CD%\metadata\20260507-120109"
set "MAX_WORKERS=24"
set "SAMPLE_ROW_LIMIT=2"
set "SAMPLE_VALUE_MAX_CHARS=160"

set "LLM=deepseek_v4_flash"
@REM Supported values: non_think, think_high, think_max
set "REASONING_MODE=non_think"
set "PROMPT_TEMPLATE_NAME=ER2Data_selection_test_v0.md"

set "LOG_ROOT=%CD%\log\er_selection_test"

if /I "%RUN_MODE%"=="all" (
  if not exist "%METADATA_DIR%" (
    echo [run_er_selection_test.bat] metadata dir not found: %METADATA_DIR%
    popd
    exit /b 1
  )

  python -m src.run.er_selection ^
    --metadata-dir "%METADATA_DIR%" ^
    --max-workers %MAX_WORKERS% ^
    --log-root "%LOG_ROOT%" ^
    --model-config "%LLM%" ^
    --reasoning-mode "%REASONING_MODE%" ^
    --sample-row-limit %SAMPLE_ROW_LIMIT% ^
    --sample-value-max-chars %SAMPLE_VALUE_MAX_CHARS% ^
    --prompt-template-name "%PROMPT_TEMPLATE_NAME%"
  if errorlevel 1 (
    echo [run_er_selection_test.bat] er selection batch failed.
    popd
    exit /b 1
  )

  popd
  exit /b 0
)

if /I "%RUN_MODE%"=="single" (
  call :run_case "%CASE_DIR%"
  if errorlevel 1 (
    popd
    exit /b 1
  )
  popd
  exit /b 0
)

echo [run_er_selection_test.bat] unknown RUN_MODE: %RUN_MODE%
echo [run_er_selection_test.bat] supported values: single, all
popd
exit /b 1

:run_case
set "CURRENT_CASE_DIR=%~1"
set "INPUT_PATH=%CURRENT_CASE_DIR%\nl2er_output.json"
set "SCHEMA_LINKING_PATH=%CURRENT_CASE_DIR%\schema_linking.json"
set "OUTPUT_PATH=%CURRENT_CASE_DIR%\er_selection.json"
set "SCHEMA_SNAPSHOT_OUTPUT_PATH=%CURRENT_CASE_DIR%\er_selection_schema_snapshot.json"
set "CASE_LOG_DIR=%LOG_ROOT%\%~nx1"

if not exist "%INPUT_PATH%" (
  echo [run_er_selection_test.bat] input not found: %INPUT_PATH%
  exit /b 1
)

if not exist "%SCHEMA_LINKING_PATH%" (
  echo [run_er_selection_test.bat] schema linking not found: %SCHEMA_LINKING_PATH%
  exit /b 1
)

echo [run_er_selection_test.bat] running case: %CURRENT_CASE_DIR%

python -m src.run.er_selection ^
  --input-path "%INPUT_PATH%" ^
  --schema-linking-path "%SCHEMA_LINKING_PATH%" ^
  --output-path "%OUTPUT_PATH%" ^
  --schema-snapshot-output-path "%SCHEMA_SNAPSHOT_OUTPUT_PATH%" ^
  --log-dir "%CASE_LOG_DIR%" ^
  --model-config "%LLM%" ^
  --reasoning-mode "%REASONING_MODE%" ^
  --sample-row-limit %SAMPLE_ROW_LIMIT% ^
  --sample-value-max-chars %SAMPLE_VALUE_MAX_CHARS% ^
  --prompt-template-name "%PROMPT_TEMPLATE_NAME%"
if errorlevel 1 (
  echo [run_er_selection_test.bat] er selection test failed: %CURRENT_CASE_DIR%
  exit /b 1
)

echo [run_er_selection_test.bat] output wrote to %OUTPUT_PATH%
echo [run_er_selection_test.bat] schema snapshot wrote to %SCHEMA_SNAPSHOT_OUTPUT_PATH%
echo [run_er_selection_test.bat] logs wrote to %CASE_LOG_DIR%
exit /b 0
