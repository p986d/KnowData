@echo off
setlocal EnableExtensions EnableDelayedExpansion

pushd "%~dp0" || exit /b 1

set "PYTHON_EXE=D:\Tools\MiniConda\envs\spider2\python.exe"

@REM Choose `single` to run specified question_id values, or `all` to traverse every question in INPUT_PATH.
set "RUN_MODE=single"

set "INPUT_PATH=%CD%\data\input.json"
@REM Multiple values can be comma-separated.
set "QUESTION_IDS=sf_bq052"

set "METADATA_ROOT=%CD%\metadata\question_resolver_diff_test"
set "LOG_ROOT=%CD%\log\question_resolver_diff_test"
set "MAX_STAGE_WORKERS=8"

set "LLM=deepseek_v4_flash"
@REM Supported values: non_think, think_high, think_max
set "REASONING_MODE=non_think"

set "QUESTION_RESOLVER_TEMPLATE_NAME=NL2ER_ERA_question_resolve_st1_v0.6.md"
set "QUESTION_RESOLVER_DIFF_CONSTRUCT_TEMPLATE_NAME=NL2ER_QuestionResolve_Diff_Construct_v0.1.md"

if not exist "%PYTHON_EXE%" (
  echo [run_temp.bat] python not found: %PYTHON_EXE%
  popd
  exit /b 1
)

if /I "%RUN_MODE%"=="all" (
  "%PYTHON_EXE%" -B -m src.run.knowdata_question_resolver_diff_stage ^
    --input-path "%INPUT_PATH%" ^
    --metadata-root "%METADATA_ROOT%" ^
    --log-root "%LOG_ROOT%" ^
    --max-stage-workers %MAX_STAGE_WORKERS% ^
    --model-config "%LLM%" ^
    --reasoning-mode "%REASONING_MODE%" ^
    --question-resolver-template-name "%QUESTION_RESOLVER_TEMPLATE_NAME%" ^
    --question-resolver-diff-construct-template-name "%QUESTION_RESOLVER_DIFF_CONSTRUCT_TEMPLATE_NAME%"
  if errorlevel 1 (
    echo [run_temp.bat] question resolver diff all run failed.
    popd
    exit /b 1
  )

  echo [run_temp.bat] question resolver diff all run completed.
  popd
  exit /b 0
)

if /I "%RUN_MODE%"=="single" (
  "%PYTHON_EXE%" -B -m src.run.knowdata_question_resolver_diff_stage ^
    --input-path "%INPUT_PATH%" ^
    --question-id "%QUESTION_IDS%" ^
    --metadata-root "%METADATA_ROOT%" ^
    --log-root "%LOG_ROOT%" ^
    --max-stage-workers %MAX_STAGE_WORKERS% ^
    --model-config "%LLM%" ^
    --reasoning-mode "%REASONING_MODE%" ^
    --question-resolver-template-name "%QUESTION_RESOLVER_TEMPLATE_NAME%" ^
    --question-resolver-diff-construct-template-name "%QUESTION_RESOLVER_DIFF_CONSTRUCT_TEMPLATE_NAME%"
  if errorlevel 1 (
    echo [run_temp.bat] question resolver diff single run failed.
    popd
    exit /b 1
  )

  echo [run_temp.bat] question resolver diff single run completed.
  popd
  exit /b 0
)

echo [run_temp.bat] unknown RUN_MODE: %RUN_MODE%
echo [run_temp.bat] supported values: single, all
popd
exit /b 1
