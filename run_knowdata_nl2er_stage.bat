@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

@REM Choose `single` to run specified question_id values, or `all` to traverse every question in INPUT_PATH.
set "RUN_MODE=single"

set "INPUT_PATH=%CD%\data\input.json"
@REM set "INPUT_PATH=%CD%\data\sy_input.json"
@REM set "INPUT_PATH=%CD%\data\sy_input_with_hint.json"
@REM Multiple values can be comma-separated.
set "QUESTION_IDS=sf_bq248"

set "METADATA_ROOT=%CD%\metadata"
set "LOG_ROOT=%CD%\log\knowdata_nl2er_stage"
set "MAX_STAGE_WORKERS=24"

set "PYTHON_EXE=D:\Tools\MiniConda\envs\spider2\python.exe"
set "LLM=deepseek_v4_flash"
@REM Supported values: non_think, think_high, think_max
set "REASONING_MODE=non_think"
set "QUESTION_RESOLVER_TEMPLATE_NAME=NL2ER_ERA_question_resolve_st1_v0.7.md"
set "NL2ER_PROMPT_TEMPLATE_NAME=NL2ER_ERA_er_extract_st2_v0.6.md"
set "DIFF_IDEA_COUNT=4"
set "DIFF_MAX_RETRY=2"
set "INCLUDE_QUESTION_AMBIGUITY_IN_ER_EXTRACT=1"
set "INCLUDE_RESOLVE_PROCESS=1"
set "STRATEGY_FOCUS="

set "QUESTION_AMBIGUITY_ARG="
if "%INCLUDE_QUESTION_AMBIGUITY_IN_ER_EXTRACT%"=="1" set "QUESTION_AMBIGUITY_ARG=--include-question-ambiguity-in-er-extract"

set "RESOLVE_PROCESS_ARG="
if "%INCLUDE_RESOLVE_PROCESS%"=="1" set "RESOLVE_PROCESS_ARG=--include-resolve-process"

if /I "%RUN_MODE%"=="all" (
  "%PYTHON_EXE%" -B -m src.run.knowdata_nl2er_stage ^
    --input-path "%INPUT_PATH%" ^
    --metadata-root "%METADATA_ROOT%" ^
    --log-root "%LOG_ROOT%" ^
    --max-stage-workers %MAX_STAGE_WORKERS% ^
    --reasoning-mode "%REASONING_MODE%" ^
    --question-resolver-template-name "%QUESTION_RESOLVER_TEMPLATE_NAME%" ^
    --nl2er-prompt-template-name "%NL2ER_PROMPT_TEMPLATE_NAME%" ^
    --nl2er-model-config "%LLM%" ^
    --diff-idea-count %DIFF_IDEA_COUNT% ^
    --diff-max-retry %DIFF_MAX_RETRY% ^
    --strategy-focus "%STRATEGY_FOCUS%" ^
    %QUESTION_AMBIGUITY_ARG% ^
    %RESOLVE_PROCESS_ARG%
  if errorlevel 1 (
    echo [run_knowdata_nl2er_stage.bat] nl2er+diff stage all run failed.
    popd
    exit /b 1
  )

  popd
  exit /b 0
)

if /I "%RUN_MODE%"=="single" (
  "%PYTHON_EXE%" -B -m src.run.knowdata_nl2er_stage ^
    --input-path "%INPUT_PATH%" ^
    --question-id "%QUESTION_IDS%" ^
    --metadata-root "%METADATA_ROOT%" ^
    --log-root "%LOG_ROOT%" ^
    --max-stage-workers %MAX_STAGE_WORKERS% ^
    --reasoning-mode "%REASONING_MODE%" ^
    --question-resolver-template-name "%QUESTION_RESOLVER_TEMPLATE_NAME%" ^
    --nl2er-prompt-template-name "%NL2ER_PROMPT_TEMPLATE_NAME%" ^
    --nl2er-model-config "%LLM%" ^
    --diff-idea-count %DIFF_IDEA_COUNT% ^
    --diff-max-retry %DIFF_MAX_RETRY% ^
    --strategy-focus "%STRATEGY_FOCUS%" ^
    %QUESTION_AMBIGUITY_ARG% ^
    %RESOLVE_PROCESS_ARG%
  if errorlevel 1 (
    echo [run_knowdata_nl2er_stage.bat] nl2er+diff stage single run failed.
    popd
    exit /b 1
  )

  popd
  exit /b 0
)

echo [run_knowdata_nl2er_stage.bat] unknown RUN_MODE: %RUN_MODE%
echo [run_knowdata_nl2er_stage.bat] supported values: single, all
popd
exit /b 1
