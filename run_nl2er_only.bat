
@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

@REM Choose `single` to run specified question_id values, or `all` to traverse every question in data\input.json.
set "RUN_MODE=single"
set "MODE=er_test_st2"
set "MAX_WORKERS=25"

set "INPUT_PATH=%CD%\data\input.json"
@REM set "INPUT_PATH=%CD%\data\sy_input.json"
@REM set "INPUT_PATH=%CD%\data\sy_input_with_hint.json"
@REM Multiple values can be comma-separated.
set "QUESTION_IDS=sf_bq050"


set "LLM=deepseek_v4_flash"
@REM Supported values: non_think, think_high, think_max
set "REASONING_MODE=non_think"

if /I "%RUN_MODE%"=="all" (
    python -m src.run.nl2er_only --mode "%MODE%" --input-path "%INPUT_PATH%" --model-config "%LLM%" --reasoning-mode "%REASONING_MODE%" --max-workers %MAX_WORKERS%
) else (
    python -m src.run.nl2er_only --mode "%MODE%" --input-path "%INPUT_PATH%" --question-id "%QUESTION_IDS%" --model-config "%LLM%" --reasoning-mode "%REASONING_MODE%" --max-workers %MAX_WORKERS%
)

popd
